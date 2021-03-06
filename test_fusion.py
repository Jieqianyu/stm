from libs.dataset.data import DATA_CONTAINER, multibatch_collate_fn
from libs.dataset.transform import TrainTransform, TestTransform
from libs.utils.logger import set_logging, AverageMeter
from libs.utils.loss import *
from libs.utils.utility import write_mask, save_checkpoint, adjust_learning_rate, mask_iou, davis2017_eval
from libs.models.fusion_models import STM

import torch
import torch.nn as nn
import torch.optim as optim
import torch.utils.data as data

import numpy as np
import pandas as pd
import os
import os.path as osp
import shutil
import time
import pickle
from progress.bar import Bar
from collections import OrderedDict
import argparse
import logging

from options import OPTION as opt

MAX_FLT = 1e6


def parse_args():
    parser = argparse.ArgumentParser('Testing Mask Segmentation')
    parser.add_argument('--checkpoint', default='', type=str, help='checkpoint to test the network')
    parser.add_argument('--gpu', default='0', type=str, help='set gpu id to test the network')

    return parser.parse_args()

def main():
    
    args = parse_args()
    set_logging(filename=os.path.join(opt.results, opt.valset, 'results.txt'), resume=True)
    logger = logging.getLogger(__name__)
    print(opt)
    # Use CUDA
    device = 'cuda:{}'.format(args.gpu)
    use_gpu = torch.cuda.is_available() and int(args.gpu) >= 0
    
    # Data
    print('==> Preparing dataset %s' % opt.valset)

    input_dim = opt.input_size

    test_transformer = TestTransform(size=input_dim)

    testset = DATA_CONTAINER[opt.valset](
        train=False, 
        transform=test_transformer, 
        samples_per_video=1
        )

    testloader = data.DataLoader(testset, batch_size=1, shuffle=False, num_workers=opt.workers,
                                 collate_fn=multibatch_collate_fn)
    # Model
    print("==> creating model")

    net = STM(opt)
    print('    Total params: %.2fM' % (sum(p.numel() for p in net.parameters())/1000000.0))

    # set eval to freeze batchnorm update
    net.eval()

    if use_gpu:
        net.to(device)

    # set training parameters
    for p in net.parameters():
        p.requires_grad = False

    # Resume
    title = 'STM'

    if args.checkpoint:
        # Load checkpoint.
        print('==> Loading checkpoint {}'.format(args.checkpoint))
        assert os.path.isfile(args.checkpoint), 'Error: no checkpoint directory found!'
        checkpoint = torch.load(args.checkpoint, map_location=device)
        state = checkpoint['state_dict']
        epoch = checkpoint['epoch']
        net.load_param(state)

    # Test
    print('==> Runing model on dataset {}, totally {:d} videos'.format(opt.valset, len(testloader)))

    test(testloader,
        model=net,
        use_cuda=use_gpu,
        device=device,
        opt=opt)

    print('==> Results are saved at: {}'.format(os.path.join(opt.results, opt.valset)))
    
    # Test davis 2017
    res = davis2017_eval(results_path=os.path.join(opt.results, opt.valset))
    log_format = 'Epoch: {} J&F: {}'
    logger.info(log_format.format(epoch, res))
    
    

def test(testloader, model, use_cuda, device, opt):

    data_time = AverageMeter()
    fps = AverageMeter()
    global_fps = AverageMeter()

    with torch.no_grad():
        for batch_idx, data in enumerate(testloader):

            frames, masks, objs, infos = data

            if use_cuda:
                frames = frames.to(device)
                masks = masks.to(device)
                
            frames = frames[0]
            masks = masks[0]
            num_objects = objs[0]
            info = infos[0]
            max_obj = masks.shape[1]-1
            T, _, H, W = frames.shape

            bar = Bar(info['name'], max=T-1)
            print('==>Runing video {}, objects {:d}'.format(info['name'], num_objects))
            # compute output
            
            pred = [masks[0:1]]
            keys = []
            vals = []
            for t in range(1, T):
                if t-1 == 0:
                    tmp_mask = masks[0:1]
                elif 'frame' in info and t-1 in info['frame']:
                    # start frame
                    mask_id = info['frame'].index(t-1)
                    tmp_mask = masks[mask_id:mask_id+1]
                    num_objects = max(num_objects, tmp_mask.max())
                else:
                    tmp_mask = out

                t1 = time.time()
                # memorize
                key, val, _ = model(frame=frames[t-1:t, :, :, :], mask=tmp_mask, num_objects=num_objects)

                # segment
                tmp_key = torch.cat(keys+[key], dim=1)
                tmp_val = torch.cat(vals+[val], dim=1)
                logits, ps = model(frame=frames[t:t+1, :, :, :], keys=tmp_key, values=tmp_val, num_objects=num_objects, max_obj=max_obj)

                out = torch.softmax(logits, dim=1)

                pred.append(out)

                if (t-1) % opt.save_freq == 0:
                    keys.append(key)
                    vals.append(val)
                # _, idx = torch.max(out, dim=1)

                toc = time.time() - t1

                data_time.update(toc, 1)
                fps.update(1/toc, 1)

                # plot progress
                bar.suffix  = '({batch}/{size}) FPS: {fps:.1f} Time: {data:.3f}s'.format(
                    batch=t,
                    size=T-1,
                    fps=fps.val,
                    data=data_time.sum
                )
                bar.next()
            bar.finish()
        
            global_fps.update((T-1)/data_time.sum)
            data_time.reset()
            
            pred = torch.cat(pred, dim=0)
            pred = pred.detach().cpu().numpy()
            write_mask(pred, info, opt)
        print("Global FPS:{:.1f}".format(global_fps.avg))

    return


if __name__ == '__main__':
    main()
