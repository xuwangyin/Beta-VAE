#! /bin/sh

python main.py --dataset celebahq128 --seed 1 --lr 1e-4 --beta1 0.9 --beta2 0.999 \
    --objective H --model H --batch_size 128 --z_dim 128 --max_iter 1.5e6 \
    --beta 1 --viz_name celebahq128_H_beta10_z128 --dset_dir ./data \
    --gather_step 1000 --display_step 5000

