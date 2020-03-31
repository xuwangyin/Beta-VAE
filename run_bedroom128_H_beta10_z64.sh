#! /bin/sh

python main.py --dataset bedroom128 --seed 1 --lr 1e-4 --beta1 0.9 --beta2 0.999 \
    --objective H --model H --batch_size 128 --z_dim 64 --max_iter 1.5e6 \
    --beta 10 --viz_name bedroom128_H_beta10_z64 --dset_dir ./data \
    --gather_step 1000 --display_step 5000

