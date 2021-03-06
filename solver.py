"""solver.py"""

import warnings
warnings.filterwarnings("ignore")

import os
from tqdm import tqdm
import visdom
import numpy as np
import socket

import torch
import torch.optim as optim
import torch.nn.functional as F
from torchvision.utils import make_grid, save_image
import torchvision.transforms as transforms

from utils import grid2gif
from model import BetaVAE_H, BetaVAE_B, WAE
from dataset import return_data
import ot
from torch.utils.tensorboard import SummaryWriter


def reconstruction_loss(x, x_recon, distribution):
    batch_size = x.size(0)
    assert batch_size != 0

    if distribution == 'bernoulli':
        recon_loss = F.binary_cross_entropy_with_logits(x_recon, x, size_average=False).div(batch_size)
    elif distribution == 'gaussian':
        x_recon = F.sigmoid(x_recon)
        recon_loss = F.mse_loss(x_recon, x, size_average=False).div(batch_size)
    else:
        recon_loss = None

    return recon_loss


def kl_divergence(mu, logvar):
    batch_size = mu.size(0)
    assert batch_size != 0
    if mu.data.ndimension() == 4:
        mu = mu.view(mu.size(0), mu.size(1))
    if logvar.data.ndimension() == 4:
        logvar = logvar.view(logvar.size(0), logvar.size(1))

    klds = -0.5*(1 + logvar - mu.pow(2) - logvar.exp())
    total_kld = klds.sum(1).mean(0, True)
    dimension_wise_kld = klds.mean(0)
    mean_kld = klds.mean(1).mean(0, True)

    return total_kld, dimension_wise_kld, mean_kld

def Wasserstein2_dist(z):
    N, ndim = z.size()
    a, b = np.ones((N,)) / N, np.ones((N,)) / N  # points have equal probability of 1/N
    prior = np.random.randn(N, ndim)
    M = ot.dist(z.data.cpu().numpy(), prior, metric='sqeuclidean')
    G = ot.emd(a, b, M, numItermax=500000)
    ix1, ix2 = np.nonzero(G)
    prior_var = torch.from_numpy(prior[ix2]).to('cuda')
    w2 = torch.sqrt(torch.mean(torch.sum(torch.pow(z - prior_var, 2), dim=1)))
    # w2 = torch.mean(torch.norm(x - z_var, p=2, dim=1))
    return w2

class DataGather(object):
    def __init__(self):
        self.data = self.get_empty_data_dict()

    def get_empty_data_dict(self):
        return dict(iter=[],
                    recon_loss=[],
                    total_kld=[],
                    dim_wise_kld=[],
                    mean_kld=[],
                    mu=[],
                    var=[],
                    images=[],
                    w2_dist=[],)

    def insert(self, **kwargs):
        for key in kwargs:
            self.data[key].append(kwargs[key])

    def flush(self):
        self.data = self.get_empty_data_dict()


class Solver(object):
    def __init__(self, args):
        self.use_cuda = args.cuda and torch.cuda.is_available()
        self.max_iter = args.max_iter
        self.global_iter = 0
        self.device = 'cuda'

        self.z_dim = args.z_dim
        self.beta = args.beta
        self.gamma = args.gamma
        self.C_max = args.C_max
        self.C_stop_iter = args.C_stop_iter
        self.objective = args.objective
        self.model = args.model
        self.lr = args.lr
        self.beta1 = args.beta1
        self.beta2 = args.beta2

        if args.dataset.lower() == 'dsprites':
            self.nc = 1
            self.decoder_dist = 'bernoulli'
        elif args.dataset.lower() == '3dchairs':
            self.nc = 3
            self.decoder_dist = 'gaussian'
        elif args.dataset.lower() == 'celeba':
            self.nc = 3
            self.decoder_dist = 'gaussian'
        elif args.dataset.lower() == 'cifar10':
            self.nc = 3
            self.decoder_dist = 'gaussian'
        elif args.dataset.lower() in ['church128', 'celebahq128', 'bedroom128', 'dog128']:
            self.nc = 3
            self.decoder_dist = 'gaussian'
        else:
            raise NotImplementedError

        if args.model == 'H':
            net = BetaVAE_H
        elif args.model == 'B':
            net = BetaVAE_B
        elif args.model == 'WAE':
            net = WAE
        else:
            raise NotImplementedError('only support model H or B')

        if args.dataset.lower() == 'cifar10':
            self.net = net(self.z_dim, self.nc, input_size=32).to(self.device)
        elif args.dataset.lower() in ['church128', 'celebahq128', 'bedroom128', 'dog128']:
            self.net = net(self.z_dim, self.nc, input_size=128).to(self.device)
        else:
            self.net = net(self.z_dim, self.nc).to(self.device)
        self.optim = optim.Adam(self.net.parameters(), lr=self.lr,
                                    betas=(self.beta1, self.beta2))

        self.viz_name = args.viz_name
        self.viz_port = args.viz_port
        self.viz_on = args.viz_on
        self.win_recon = None
        self.win_kld = None
        self.win_w2_dist = None
        self.win_mu = None
        self.win_var = None

        self.ckpt_dir = os.path.join(args.ckpt_dir, args.viz_name)
        if not os.path.exists(self.ckpt_dir):
            os.makedirs(self.ckpt_dir, exist_ok=True)
        self.ckpt_name = args.ckpt_name
        if self.ckpt_name is not None:
            self.load_checkpoint(self.ckpt_name)

        self.save_output = args.save_output
        self.output_dir = os.path.join(args.output_dir, args.viz_name)
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)

        self.writer = SummaryWriter(self.output_dir, flush_secs=10)

        self.gather_step = args.gather_step
        self.display_step = args.display_step
        self.save_step = args.save_step

        self.dset_dir = args.dset_dir
        self.dataset = args.dataset
        self.batch_size = args.batch_size
        self.data_loader = return_data(args)

        self.test_batch = next(iter(self.data_loader)).to(self.device)

        self.gather = DataGather()

    def train(self):
        self.net_mode(train=True)
        self.C_max = torch.FloatTensor([self.C_max]).to(self.device)
        out = False

        pbar = tqdm(total=self.max_iter)
        pbar.update(self.global_iter)
        while not out:
            for x in self.data_loader:
                self.global_iter += 1
                pbar.update(1)

                x = x.to(self.device)

                if self.model in ['H', 'B']:
                    x_recon, mu, logvar = self.net(x)
                    recon_loss = reconstruction_loss(x, x_recon, self.decoder_dist)
                    total_kld, dim_wise_kld, mean_kld = kl_divergence(mu, logvar)

                    if self.objective == 'H':
                        loss = recon_loss + self.beta*total_kld
                    elif self.objective == 'B':
                        C = torch.clamp(self.C_max/self.C_stop_iter*self.global_iter, 0, self.C_max.item())
                        loss = recon_loss + self.gamma*(total_kld-C).abs()
                elif self.model == 'WAE':
                    x_recon, z = self.net(x)
                    recon_loss = reconstruction_loss(x, x_recon, self.decoder_dist)
                    w2_dist = Wasserstein2_dist(z)
                    loss = recon_loss + w2_dist

                self.optim.zero_grad()
                loss.backward()
                self.optim.step()

                if self.viz_on and self.global_iter%self.gather_step == 0:
                    self.writer.add_scalar('recon-loss', recon_loss.item(), self.global_iter)
                    if self.model == 'WAE':
                        self.writer.add_scalar('W2-dist', w2_dist.item(), self.global_iter)
                    else:
                        self.writer.add_scalar('mean-kld', mean_kld.item(), self.global_iter)
                        # self.gather.insert(iter=self.global_iter,
                        #                    mu=mu.mean(0).data, var=logvar.exp().mean(0).data,
                        #                    recon_loss=recon_loss.data, total_kld=total_kld.data,
                        #                    dim_wise_kld=dim_wise_kld.data, mean_kld=mean_kld.data)

                if self.global_iter%self.display_step == 0:
                    if self.model == 'WAE':
                        pbar.write('[{}] recon_loss:{:.3f} w2_dist:{:.3f}'.format(
                            self.global_iter, recon_loss.item(), w2_dist.item()))
                    else:
                        pbar.write('[{}] recon_loss:{:.3f} total_kld:{:.3f} mean_kld:{:.3f}'.format(
                            self.global_iter, recon_loss.item(), total_kld.item(), mean_kld.item()))

                    # var = logvar.exp().mean(0).data
                    # var_str = ''
                    # for j, var_j in enumerate(var):
                    #     var_str += 'var{}:{:.4f} '.format(j+1, var_j)
                    # pbar.write(var_str)

                    # if self.objective == 'B':
                    #     pbar.write('C:{:.3f}'.format(C.item()))

                    if self.viz_on:
                        self.viz_reconstruction()
                        # self.viz_lines()
                        self.viz_rand_samples()
                        # self.gather.flush()

                    # if self.viz_on or self.save_output:
                    #     self.viz_traverse()

                if self.global_iter%self.save_step == 0:
                    self.save_checkpoint('last')
                    pbar.write('Saved checkpoint(iter:{})'.format(self.global_iter))

                if self.global_iter%50000 == 0:
                    self.save_checkpoint(str(self.global_iter))

                if self.global_iter >= self.max_iter:
                    out = True
                    break

        pbar.write("[Training Finished]")
        pbar.close()

    def viz_reconstruction(self):
        self.net_mode(train=False)
        x_recon, mu, logvar = self.net(self.test_batch)
        x_recon = F.sigmoid(x_recon)
        images = make_grid(torch.cat([self.test_batch[:8], x_recon[:8]]).cpu(), nrow=8)
        self.writer.add_image('recons', images, self.global_iter)
        self.net_mode(train=True)

    def viz_lines(self):
        self.net_mode(train=False)
        recon_losses = torch.stack(self.gather.data['recon_loss']).cpu()

        iters = torch.Tensor(self.gather.data['iter'])

        if self.model == 'WAE':
            w2_dist = torch.Tensor(self.gather.data['w2_dist']).cpu()
        else:
            mus = torch.stack(self.gather.data['mu']).cpu()
            vars = torch.stack(self.gather.data['var']).cpu()

            dim_wise_klds = torch.stack(self.gather.data['dim_wise_kld'])
            mean_klds = torch.stack(self.gather.data['mean_kld'])
            total_klds = torch.stack(self.gather.data['total_kld'])
            klds = torch.cat([dim_wise_klds, mean_klds, total_klds], 1).cpu()

            legend = []
            for z_j in range(self.z_dim):
                legend.append('z_{}'.format(z_j))
            legend.append('mean')
            legend.append('total')

        if self.win_recon is None:
            self.win_recon = self.viz.line(
                                        X=iters,
                                        Y=recon_losses,
                                        env=self.viz_name+'_lines',
                                        opts=dict(
                                            width=400,
                                            height=400,
                                            xlabel='iteration',
                                            title='reconsturction loss',))
        else:
            self.win_recon = self.viz.line(
                                        X=iters,
                                        Y=recon_losses,
                                        env=self.viz_name+'_lines',
                                        win=self.win_recon,
                                        update='append',
                                        opts=dict(
                                            width=400,
                                            height=400,
                                            xlabel='iteration',
                                            title='reconsturction loss',))
        if self.model == 'WAE':
            if self.win_w2_dist is None:
                self.win_w2_dist = self.viz.line(
                                            X=iters,
                                            Y=w2_dist,
                                            env=self.viz_name+'_lines',
                                            opts=dict(
                                                width=400,
                                                height=400,
                                                xlabel='iteration',
                                                title='Wasserstein2 distance',))
            else:
                self.win_w2_dist = self.viz.line(
                                            X=iters,
                                            Y=w2_dist,
                                            env=self.viz_name+'_lines',
                                            win=self.win_w2_dist,
                                            update='append',
                                            opts=dict(
                                                width=400,
                                                height=400,
                                                xlabel='iteration',
                                                title='Wasserstein2 divergence',))
        else:
            if self.win_kld is None:
                self.win_kld = self.viz.line(
                                            X=iters,
                                            Y=klds,
                                            env=self.viz_name+'_lines',
                                            opts=dict(
                                                width=400,
                                                height=400,
                                                legend=legend,
                                                xlabel='iteration',
                                                title='kl divergence',))
            else:
                self.win_kld = self.viz.line(
                                            X=iters,
                                            Y=klds,
                                            env=self.viz_name+'_lines',
                                            win=self.win_kld,
                                            update='append',
                                            opts=dict(
                                                width=400,
                                                height=400,
                                                legend=legend,
                                                xlabel='iteration',
                                                title='kl divergence',))

            if self.win_mu is None:
                self.win_mu = self.viz.line(
                                            X=iters,
                                            Y=mus,
                                            env=self.viz_name+'_lines',
                                            opts=dict(
                                                width=400,
                                                height=400,
                                                legend=legend[:self.z_dim],
                                                xlabel='iteration',
                                                title='posterior mean',))
            else:
                self.win_mu = self.viz.line(
                                            X=iters,
                                            Y=vars,
                                            env=self.viz_name+'_lines',
                                            win=self.win_mu,
                                            update='append',
                                            opts=dict(
                                                width=400,
                                                height=400,
                                                legend=legend[:self.z_dim],
                                                xlabel='iteration',
                                                title='posterior mean',))

            if self.win_var is None:
                self.win_var = self.viz.line(
                                            X=iters,
                                            Y=vars,
                                            env=self.viz_name+'_lines',
                                            opts=dict(
                                                width=400,
                                                height=400,
                                                legend=legend[:self.z_dim],
                                                xlabel='iteration',
                                                title='posterior variance',))
            else:
                self.win_var = self.viz.line(
                                            X=iters,
                                            Y=vars,
                                            env=self.viz_name+'_lines',
                                            win=self.win_var,
                                            update='append',
                                            opts=dict(
                                                width=400,
                                                height=400,
                                                legend=legend[:self.z_dim],
                                                xlabel='iteration',
                                                title='posterior variance',))
        self.net_mode(train=True)

    def viz_rand_samples(self):
        np.random.seed(123)
        z = torch.from_numpy(np.random.randn(36, self.z_dim).astype(np.float32)).to(self.device)
        self.net_mode(train=False)
        with torch.no_grad():
            samples = F.sigmoid(self.net.decoder(z)).cpu()
        self.writer.add_image('rand_samples', make_grid(samples, nrow=6), self.global_iter)

        self.net_mode(train=True)

    def viz_traverse(self, limit=3, inter=2/3, loc=-1):
        self.net_mode(train=False)
        import random

        decoder = self.net.decoder
        encoder = self.net.encoder
        interpolation = torch.arange(-limit, limit+0.1, inter)

        n_dsets = len(self.data_loader.dataset)
        rand_idx = random.randint(1, n_dsets-1)

        random_img = self.data_loader.dataset.__getitem__(rand_idx)
        random_img = random_img.unsqueeze(0).to(self.device)
        random_img_z = encoder(random_img)[:, :self.z_dim]

        random_z = torch.rand(1, self.z_dim, device=self.device)

        if self.dataset == 'dsprites':
            fixed_idx1 = 87040 # square
            fixed_idx2 = 332800 # ellipse
            fixed_idx3 = 578560 # heart

            fixed_img1 = self.data_loader.dataset.__getitem__(fixed_idx1).to(self.device).unsqueeze(0)
            fixed_img_z1 = encoder(fixed_img1)[:, :self.z_dim]

            fixed_img2 = self.data_loader.dataset.__getitem__(fixed_idx2).to(self.device).unsqueeze(0)
            fixed_img_z2 = encoder(fixed_img2)[:, :self.z_dim]

            fixed_img3 = self.data_loader.dataset.__getitem__(fixed_idx3).to(self.device).unsqueeze(0)
            fixed_img_z3 = encoder(fixed_img3)[:, :self.z_dim]

            Z = {'fixed_square':fixed_img_z1, 'fixed_ellipse':fixed_img_z2,
                 'fixed_heart':fixed_img_z3, 'random_img':random_img_z}
        else:
            fixed_idx = 0
            fixed_img = self.data_loader.dataset.__getitem__(fixed_idx).to(self.device).unsqueeze(0)
            fixed_img_z = encoder(fixed_img)[:, :self.z_dim]

            Z = {'fixed_img':fixed_img_z, 'random_img':random_img_z, 'random_z':random_z}

        gifs = []
        for key in Z.keys():
            z_ori = Z[key]
            samples = []
            for row in range(self.z_dim):
                if loc != -1 and row != loc:
                    continue
                z = z_ori.clone()
                for val in interpolation:
                    z[:, row] = val
                    sample = F.sigmoid(decoder(z)).data
                    samples.append(sample)
                    gifs.append(sample)
            samples = torch.cat(samples, dim=0).cpu()
            title = '{}_latent_traversal(iter:{})'.format(key, self.global_iter)

            if self.viz_on:
                self.viz.images(samples, env=self.viz_name+'_traverse',
                                opts=dict(title=title), nrow=len(interpolation))

        if self.save_output:
            output_dir = os.path.join(self.output_dir, str(self.global_iter))
            os.makedirs(output_dir, exist_ok=True)
            gifs = torch.cat(gifs)
            if self.dataset == 'cifar10':
                gifs = gifs.view(len(Z), self.z_dim, len(interpolation), self.nc, 32, 32).transpose(1, 2)
            elif self.dataset in ['church128', 'celebahq128', 'bedroom128', 'dog128']:
                gifs = gifs.view(len(Z), self.z_dim, len(interpolation), self.nc, 128, 128).transpose(1, 2)
            else:
                gifs = gifs.view(len(Z), self.z_dim, len(interpolation), self.nc, 64, 64).transpose(1, 2)
            for i, key in enumerate(Z.keys()):
                for j, val in enumerate(interpolation):
                    save_image(tensor=gifs[i][j].cpu(),
                               fp=os.path.join(output_dir, '{}_{}.jpg'.format(key, j)),
                               nrow=self.z_dim, pad_value=1)

                grid2gif(os.path.join(output_dir, key+'*.jpg'),
                         os.path.join(output_dir, key+'.gif'), delay=10)

        self.net_mode(train=True)

    def rand_samples(self, num_samples):
        import numpy as np
        from PIL import Image
        import matplotlib.pyplot as plt
        self.net_mode(train=False)
        np.random.seed(123)
        z = torch.from_numpy(np.random.randn(num_samples, self.z_dim).astype(np.float32)).to(self.device)
        # z = torch.randn(num_samples, self.z_dim, device=self.device)
        with torch.no_grad():
            out = F.sigmoid(self.net.decoder(z))
        self.net_mode(train=True)
        out = out.cpu()
        grid = make_grid(out[:36], nrow=6, normalize=True)
        plt.imshow(transforms.ToPILImage()(grid))
        plt.show()
        out = out.numpy().transpose([0, 2, 3, 1])
        out = (out * 255).astype(np.uint8)
        if self.dataset in ['cifar10', 'church128', 'celebahq128', 'bedroom128', 'dog128']:
            np.save('img_seed_{}_betavae.npy'.format(self.dataset), out)
            return out
        else:
            out128 = []
            for i in range(out.shape[0]):
                im = Image.fromarray(out[i]).resize([128, 128], resample=Image.LANCZOS)
                out128.append(np.array(im))
            out128 = np.stack(out128, axis=0)
            np.save('img_seed_celebahq128_betavae.npy', out128)
            return out128


    def net_mode(self, train):
        if not isinstance(train, bool):
            raise('Only bool type is supported. True or False')

        if train:
            self.net.train()
        else:
            self.net.eval()

    def save_checkpoint(self, filename, silent=True):
        model_states = {'net':self.net.state_dict(),}
        optim_states = {'optim':self.optim.state_dict(),}
        win_states = {'recon':self.win_recon,
                      'kld':self.win_kld,
                      'mu':self.win_mu,
                      'var':self.win_var,}
        states = {'iter':self.global_iter,
                  'win_states':win_states,
                  'model_states':model_states,
                  'optim_states':optim_states}

        file_path = os.path.join(self.ckpt_dir, filename)
        with open(file_path, mode='wb+') as f:
            torch.save(states, f)
        if not silent:
            print("=> saved checkpoint '{}' (iter {})".format(file_path, self.global_iter))

    def load_checkpoint(self, filename):
        file_path = os.path.join(self.ckpt_dir, filename)
        if os.path.isfile(file_path):
            checkpoint = torch.load(file_path)
            self.global_iter = checkpoint['iter']
            self.win_recon = checkpoint['win_states']['recon']
            self.win_kld = checkpoint['win_states']['kld']
            self.win_var = checkpoint['win_states']['var']
            self.win_mu = checkpoint['win_states']['mu']
            self.net.load_state_dict(checkpoint['model_states']['net'])
            self.optim.load_state_dict(checkpoint['optim_states']['optim'])
            print("=> loaded checkpoint '{} (iter {})'".format(file_path, self.global_iter))
        else:
            print("=> no checkpoint found at '{}'".format(file_path))
