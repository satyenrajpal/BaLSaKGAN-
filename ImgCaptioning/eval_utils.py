from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import torch.nn as nn
from torch.autograd import Variable

import numpy as np
import json
from json import encoder
import random
import string
import time
import os
import sys
import misc.utils as utils
from torchvision import transforms as trn
preprocess = trn.Compose([
        #trn.ToTensor(),
        trn.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
])
def language_eval(dataset, preds, model_id, split):
    import sys
    if 'coco' in dataset:
        sys.path.append("coco-caption")
        annFile = 'coco-caption/annotations/captions_val2014.json'
    else:
        sys.path.append("f30k-caption")
        annFile = 'f30k-caption/annotations/dataset_flickr30k.json'
    from pycocotools.coco import COCO
    from pycocoevalcap.eval import COCOEvalCap

    encoder.FLOAT_REPR = lambda o: format(o, '.3f')

    if not os.path.isdir('eval_results'):
        os.mkdir('eval_results')
    cache_path = os.path.join('eval_results/', model_id + '_' + split + '.json')

    coco = COCO(annFile)
    valids = coco.getImgIds()

    # filter results to only those in MSCOCO validation set (will be about a third)
    preds_filt = [p for p in preds if p['image_id'] in valids]
    print('using %d/%d predictions' % (len(preds_filt), len(preds)))
    json.dump(preds_filt, open(cache_path, 'w')) # serialize to temporary json file. Sigh, COCO API...

    cocoRes = coco.loadRes(cache_path)
    cocoEval = COCOEvalCap(coco, cocoRes)
    cocoEval.params['image_id'] = cocoRes.getImgIds()
    cocoEval.evaluate()

    # create output dictionary
    out = {}
    for metric, score in cocoEval.eval.items():
        out[metric] = score

    imgToEval = cocoEval.imgToEval
    for p in preds_filt:
        image_id, caption = p['image_id'], p['caption']
        imgToEval[image_id]['caption'] = caption
    with open(cache_path, 'w') as outfile:
        json.dump({'overall': out, 'imgToEval': imgToEval}, outfile)

    return out

def eval_split(model, crit, loader, eval_kwargs={}):
    verbose = eval_kwargs.get('verbose', True)#
    num_images = eval_kwargs.get('num_images', eval_kwargs.get('val_images_use', -1))#
    split = eval_kwargs.get('split', 'val')#
    lang_eval = eval_kwargs.get('language_eval', 0)#
    dataset = eval_kwargs.get('dataset', 'coco')#
    beam_size = eval_kwargs.get('beam_size', 1)

    # Make sure in the evaluation mode
    model.eval()

    loader.reset_iterator(split)#

    n = 0
    loss = 0
    loss_sum = 0
    loss_evals = 1e-8
    predictions = []
    while True:
        data = loader.get_batch(split) #basically number of images
        n = n + loader.batch_size

        if data.get('labels', None) is not None:
            # forward the model to get loss
            tmp = [data['fc_feats'], data['att_feats'], data['labels'], data['masks']]
            tmp = [Variable(torch.from_numpy(_), volatile=True).cuda() for _ in tmp]
            fc_feats, att_feats, labels, masks = tmp

            loss = crit(model(fc_feats, att_feats, labels), labels[:,1:], masks[:,1:]).data[0]
            loss_sum = loss_sum + loss
            loss_evals = loss_evals + 1

        # forward the model to also get generated samples for each image
        # Only leave one feature for each image, in case duplicate sample
        tmp = [data['fc_feats'][np.arange(loader.batch_size) * loader.seq_per_img], 
            data['att_feats'][np.arange(loader.batch_size) * loader.seq_per_img]]
        tmp = [Variable(torch.from_numpy(_), volatile=True).cuda() for _ in tmp]
        fc_feats, att_feats = tmp
        # forward the model to also get generated samples for each image
        seq, _, h_sent = model.sample(fc_feats, att_feats, eval_kwargs) #Dont need to worry about this
        #set_trace()
        sents = utils.decode_sequence(loader.get_vocab(), seq)#

        for k, sent in enumerate(sents):
            #Creates a dictionary og images and captions #Can Directly omit this below shit
            entry = {'image_id': data['infos'][k]['id'], 'caption': sent}
            if eval_kwargs.get('dump_path', 0) == 1:
                entry['file_name'] = data['infos'][k]['file_path']
            predictions.append(entry)
            if eval_kwargs.get('dump_images', 0) == 1:
                # dump the raw image to vis/ folder
                cmd = 'cp "' + os.path.join(eval_kwargs['image_root'], data['infos'][k]['file_path']) + '" vis/imgs/img' + str(len(predictions)) + '.jpg' # bit gross
                print(cmd)
                os.system(cmd)

            if verbose:
                print('image %s: %s' %(entry['image_id'], entry['caption']))

        # if we wrapped around the split or used up val imgs budget then bail
        ix0 = data['bounds']['it_pos_now']
        ix1 = data['bounds']['it_max']
        if num_images != -1:
            ix1 = min(ix1, num_images)
        for i in range(n - ix1):
            predictions.pop()

        if verbose:
            print('evaluating validation preformance... %d/%d (%f)' %(ix0 - 1, ix1, loss))

        if data['bounds']['wrapped']:
            break
        if num_images >= 0 and n >= num_images:
            break

    lang_stats = None
    if lang_eval == 1:
        lang_stats = language_eval(dataset, predictions, eval_kwargs['id'], split)

    # Switch back to training mode
    model.train()
    return loss_sum/loss_evals, predictions, lang_stats

def get_features(imgs,my_resnet):
    batch_size=imgs.size()[0] #Not so sure about this
    fc_batch = np.ndarray((batch_size, 2048), dtype = 'float32')
    att_batch = np.ndarray((batch_size, 14, 14, 2048), dtype = 'float32')
    # infos = []
    imgs_tn=imgs.data
    imgs_tn = torch.add(imgs_tn, 1)/2
    #print("Before getting batches: ", imgs_tn.size())
    fc_batch, att_batch = my_resnet(Variable(imgs_tn))
    
    #for i in range(batch_size):
        # print("Size of image tensir full: ", imgs_tn.size())
    #    img = imgs_tn[i]
        #print("Size of the each tensor: ", img.size())
################################################
            # img = img.astype('float32')/255.0 #Check if 255 is required or not. Img from StackGAN might be from 0 to 1
            # img = torch.from_numpy(img.transpose([2,0,1])).cuda() #
        
    #    img = Variable(preprocess(img), volatile=True)
        #img = Variable(preprocess(img), volatile=True)
    #    tmp_fc, tmp_att = my_resnet(img)

    #    fc_batch[i] = tmp_fc.data.cpu().float().numpy()
    #    att_batch[i] = tmp_att.data.cpu().float().numpy()

    #print("FC_batch size: ", fc_batch.size())
    #print("AT_batch_size: ", att_batch.suze())
    data = {}
    data['fc_feats'] = fc_batch
    data['att_feats'] = att_batch
###################################################
    return fc_batch, att_batch


def captioning_model(imgs,model,vocab,my_resnet,eval_kwargs={}):
    #Load the model first
    verbose = eval_kwargs.get('verbose', True)#
    num_images = eval_kwargs.get('num_images', eval_kwargs.get('val_images_use', -1))#
    split = eval_kwargs.get('split', 'val')#
    lang_eval = eval_kwargs.get('language_eval', 0)#
    dataset = eval_kwargs.get('dataset', 'coco')#
    beam_size = eval_kwargs.get('beam_size', 1)
    batch_size=imgs.size()[0]

    # print("Incoming batch size: ", batch_size)
    # print("coming into captioning model")

    # Make sure in the evaluation mode
    model.eval()

    # loader.reset_iterator(split)#
    fc_feats, att_feats=get_features(imgs,my_resnet)
    fc_feats = fc_feats.contiguous()    
    att_feats = att_feats.contiguous()
    # print("got the features in cm")

    n = 0
    loss = 0
    loss_sum = 0
    loss_evals = 1e-8
    predictions = []
    seq_per_img = 1
    # while True:
        # forward the model to also get generated samples for each image
        # Only leave one feature for each image, in case duplicate sample
    #tmp = [data['fc_feats'][np.arange(batch_size) * seq_per_img], 
    #    data['att_feats'][np.arange(batch_size) * seq_per_img]]
    #tmp = [Variable(torch.from_numpy(_), volatile=True).cuda() for _ in tmp]
    #fc_feats, att_feats = data
    #print("\n\n\n\n\nIn captioning model\n\n\n\n\n")
    #print("Type: ", type(fc_feats))
    #print("FC_feats size: ", fc_feats.size())
    #print("Att_feats size: ", att_feats.size())
    
    # forward the model to also get generated samples for each image
    seq,h_sent = model.sample(fc_feats, att_feats, eval_kwargs) #Dont need to worry about this
    # print("hidden State shape:",h_sent.size())
    sents = utils.decode_sequence(vocab, seq)
    # for sent in sents:
    #     print(sent)
        # for k, sent in enumerate(sents):
        #     #Creates a dictionary og images and captions #Can Directly omit this below shit
        #     entry = {'image_id': data['infos'][k]['id'], 'caption': sent}
        #     if eval_kwargs.get('dump_path', 0) == 1:
        #         entry['file_name'] = data['infos'][k]['file_path']
        #     predictions.append(entry)
        #     if eval_kwargs.get('dump_images', 0) == 1:
        #         # dump the raw image to vis/ folder
        #         cmd = 'cp "' + os.path.join(eval_kwargs['image_root'], data['infos'][k]['file_path']) + '" vis/imgs/img' + str(len(predictions)) + '.jpg' # bit gross
        #         print(cmd)
        #         os.system(cmd)

            # if verbose:
            #     print('image %s: %s' %(entry['image_id'], entry['caption']))

        # # if we wrapped around the split or used up val imgs budget then bail
        # ix0 = data['bounds']['it_pos_now']
        # ix1 = data['bounds']['it_max']
    #     if num_images != -1:
    #         ix1 = min(ix1, num_images)
    #     for i in range(n - ix1):
    #         predictions.pop()

    #     if verbose:
    #         print('evaluating validation preformance... %d/%d (%f)' %(ix0 - 1, ix1, loss))

    #     if data['bounds']['wrapped']:
    #         break
    #     if num_images >= 0 and n >= num_images:
    #         break

    # lang_stats = None
    # if lang_eval == 1:
    #     lang_stats = language_eval(dataset, predictions, eval_kwargs['id'], split)

    # Switch back to training mode
    # model.train()
    # return loss_sum/loss_evals, predictions, lang_stats
    return sents,h_sent.cpu().data.numpy()
