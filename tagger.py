from config import Configuration
from load import load_embeddings
from load import load_data
from modules.feature import Feature
from modules.encoder import Encoder
from modules.mldecoder import MLDecoder
from modules.indp import INDP
from modules.rltrain import RLTrain
from modules.crf import CRF
from random import shuffle
from itertools import ifilter
import torch
import torch.optim as optim
import numpy as np
import random
import re
import os
import sys
import time
import codecs

reload(sys)
sys.setdefaultencoding('utf-8')

hasCuda = torch.cuda.is_available()

#Global variables for modules.
feature = None
encoder = None
indp = None
crf = None
mldecoder = None
rltrain = None

#Optimizers for modules.
f_opt = None
e_opt = None
i_opt = None
c_opt = None
m_opt = None
r_opt = None

def batch_to_tensors(cfg, in_B):
    o_B = {}
    o_B['ch'] = torch.LongTensor(in_B['ch'])
    o_B['rev_ch'] = torch.LongTensor(in_B['rev_ch'])
    o_B['w_len'] = torch.LongTensor(in_B['w_len'])
    o_B['w'] = torch.LongTensor(in_B['w'])
    o_B['w_chs'] = torch.LongTensor(in_B['w_chs'])
    o_B['w_cap'] = torch.LongTensor(in_B['w_cap'])
    o_B['w_mask'] = torch.FloatTensor(in_B['w_mask'])
    o_B['s_len'] = torch.LongTensor(in_B['s_len'])

    if in_B['tag'] is not None:
        o_B['tag'] = torch.LongTensor(in_B['tag'])
    else:
        o_B['tag'] = None

    if in_B['tag'] is not None:
        tag_one_hot = np.zeros((cfg.d_batch_size * cfg.max_s_len, cfg.tag_size))
        tag_one_hot[np.arange(cfg.d_batch_size * cfg.max_s_len), np.reshape(in_B['tag'], (-1,))] = 1.0
        tag_o_h = np.reshape(tag_one_hot, (cfg.d_batch_size, cfg.max_s_len, cfg.tag_size))
        o_B['tag_o_h'] = torch.FloatTensor(tag_o_h)
    else:
        o_B['tag_o_h'] = None

    cfg.B = o_B
    return

def save_predictions(cfg, batch, preds, f):
    """Saves predictions to the provided file stream."""
    #Sentence index
    s_idx = 0
    for pred in preds:
        #Word index inside sentence
        w_idx = 0
        while(w_idx < batch['s_len'][s_idx]):
            #w is the word for which we predict a tag
            w = batch['raw_w'][s_idx][w_idx]

            #tag_id is the predicted tag for w
            tag_id = pred[w_idx]
            pred_str = cfg.data['id_tag'][tag_id]

            if cfg.local_mode=='dev':
                #gold tag for dev set.
                gtag = batch['tag'][s_idx][w_idx]
                gtag_str = cfg.data['id_tag'][gtag]
                f.write(w + '\t' + gtag_str + '\t' + pred_str + '\n')

            else:
                f.write(w + '\t' + pred_str + '\n')

            #Go to the next word in the sentence
            w_idx += 1

        #Go to the next sentence
        f.write('\n')
        s_idx += 1

    return

#Used to evaluate model's performance on the dev set w.r.t. top1 tagging accuracy.
def accuracy(ref_file, pred_file):
    #Top1 Accuracy
    ref_lines = codecs.open(ref_file, 'r', 'utf-8').readlines()
    pred_lines = codecs.open(pred_file, 'r', 'utf-8').readlines()

    if len(ref_lines)!=len(pred_lines):
        print "INFO: Wrong number of lines in reference and prediction files"
        exit()

    total = 0.0
    correct = 0.0
    for index in range(len(ref_lines)):
        ref_line = ref_lines[index].strip()
        pred_line = pred_lines[index].strip()
        if len(ref_line)!=0 and len(pred_line)!=0:
            Gtags = ref_line.split('\t')
            tag = pred_line.split('\t')[2]
            total += 1
            for gtag in Gtags:
                if gtag==tag:
                    correct += 1
                    break

    return float(correct/total) * 100

#Only for NER.
def fscore(cfg):
    os.system("%s -d '\t' < %s > %s" % ('./evaluate/conlleval', 'temp.predicted_' + cfg.model_type, 'temp.score_' + cfg.model_type))
    result_lines = [line.rstrip() for line in codecs.open('temp.score_' + cfg.model_type, 'r', 'utf-8')]
    return float(result_lines[1].strip().split()[-1])

def evaluate(cfg, ref_file, pred_file):
    if cfg.task=='en_NER' or cfg.task=='de_NER':
        return fscore(cfg)
    else:
        return accuracy(ref_file, pred_file)

def run_epoch(cfg):
    cfg.local_mode = 'train'

    total_loss = []
    if cfg.model_type=='AC-RNN':
        vtotal_loss = []

    #Turn on training mode which enables dropout.
    feature.train()
    encoder.train()
    if cfg.model_type=='INDP': indp.train()
    elif cfg.model_type=='CRF': crf.train()
    else:
        mldecoder.train()
        if cfg.model_type=='AC-RNN':
            rltrain.train()

    batches = [batch for batch in load_data(cfg)]
    shuffle(batches)
    for step, batch in enumerate(batches):
        cfg.d_batch_size = batch['d_batch_size']
        cfg.max_s_len = batch['max_s_len']
        cfg.max_w_len = batch['max_w_len']

        f_opt.zero_grad()
        e_opt.zero_grad()
        if cfg.model_type=='INDP':
            i_opt.zero_grad()
        elif cfg.model_type=='CRF':
            c_opt.zero_grad()
        elif cfg.model_type=='AC-RNN':
            r_opt.zero_grad()
            m_opt.zero_grad()
        else:
            m_opt.zero_grad()

        batch_to_tensors(cfg, batch)
        F = feature()
        H = encoder(F)
        if cfg.model_type=='INDP':
            log_probs = indp(H)
            loss = indp.loss(log_probs)
        elif cfg.model_type=='CRF':
            log_probs = crf(H)
            loss = crf.loss(log_probs)
        elif cfg.model_type=='AC-RNN':
            loss, vloss = rltrain(H, mldecoder)
        else:
            log_probs = mldecoder(H)
            loss = mldecoder.loss(log_probs)

        loss.backward()
        if cfg.model_type=='AC-RNN': vloss.backward()

        torch.nn.utils.clip_grad_norm(encoder.parameters(), cfg.max_gradient_norm)
        torch.nn.utils.clip_grad_norm(feature.parameters(), cfg.max_gradient_norm)
        if cfg.model_type=='INDP': torch.nn.utils.clip_grad_norm(indp.parameters(), cfg.max_gradient_norm)
        elif cfg.model_type=='CRF': torch.nn.utils.clip_grad_norm(crf.parameters(), cfg.max_gradient_norm)
        elif cfg.model_type=='AC-RNN':
            torch.nn.utils.clip_grad_norm(mldecoder.parameters(), cfg.max_gradient_norm)
        else:
            torch.nn.utils.clip_grad_norm(mldecoder.parameters(), cfg.max_gradient_norm)

        f_opt.step()
        e_opt.step()
        if cfg.model_type=='INDP':
            i_opt.step()
        elif cfg.model_type=='CRF':
            c_opt.step()
        elif cfg.model_type=='AC-RNN':
            r_opt.step()
            m_opt.step()
        else:
            m_opt.step()

        loss_value = loss.cpu().data.numpy()[0]
        total_loss.append(loss_value)
        if cfg.model_type=='AC-RNN':
            vloss_value = vloss.cpu().data.numpy()[0]
            vtotal_loss.append(vloss_value)
            ##
            sys.stdout.write('\rBatch:{} | Loss:{} | Mean Loss:{} | VLoss:{} | Mean VLoss:{}'.format(
                                                step,
                                                loss_value,
                                                np.mean(total_loss),
                                                vloss_value,
                                                np.mean(vtotal_loss)
                                                )
                            )
            sys.stdout.flush()
        else:
            ##
            sys.stdout.write('\rBatch:{} | Loss:{} | Mean Loss:{}'.format(
                                                step,
                                                loss_value,
                                                np.mean(total_loss)
                                                )
                            )
            sys.stdout.flush()
    return

def predict(cfg, o_file):
    if cfg.mode=='train':
        cfg.local_mode = 'dev'

    elif cfg.mode=='test':
        cfg.local_mode = 'test'

    #Turn on evaluation mode which disables dropout.
    feature.eval()
    encoder.eval()
    if cfg.model_type=='INDP': indp.eval()
    elif cfg.model_type=='CRF': crf.eval()
    elif cfg.model_type=='AC-RNN':
        rltrain.eval()
        mldecoder.eval()
    else:
        mldecoder.eval()

    #file stream to save predictions
    f = codecs.open(o_file, 'w', 'utf-8')
    for batch in load_data(cfg):
        cfg.d_batch_size = batch['d_batch_size']
        cfg.max_s_len = batch['max_s_len']
        cfg.max_w_len = batch['max_w_len']
        batch_to_tensors(cfg, batch)
        F = feature()
        H = encoder(F)
        if cfg.model_type=='INDP':
            preds = indp.predict(H)[0].cpu().data.numpy()
        elif cfg.model_type=='CRF':
            preds = crf.predict(H)
        else:
            if cfg.search=='greedy':
                preds = mldecoder.greedy(H)[0].cpu().data.numpy()
            elif cfg.search=='beam':
                preds = mldecoder.beam(H)[0][:,0,:].cpu().data.numpy()

        save_predictions(cfg, batch, preds, f)

    f.close()
    return

def run_model(mode, path, in_file, o_file):
    global feature, encoder, indp, crf, mldecoder, rltrain, f_opt, e_opt, i_opt, c_opt, m_opt, r_opt


    cfg = Configuration()

    #General mode has two values: 'train' or 'test'
    cfg.mode = mode

    #Set Random Seeds
    random.seed(cfg.seed)
    np.random.seed(cfg.seed)
    torch.manual_seed(cfg.seed)
    if hasCuda:
        torch.cuda.manual_seed_all(cfg.seed)

    #Load Embeddings
    load_embeddings(cfg)

    #Only for testing
    if mode=='test': cfg.test_raw = in_file

    #Construct models
    feature = Feature(cfg)
    if cfg.model_type=='AC-RNN':
        f_opt = optim.SGD(ifilter(lambda p: p.requires_grad, feature.parameters()), lr=cfg.actor_step_size)
    else:
        f_opt = optim.Adam(ifilter(lambda p: p.requires_grad, feature.parameters()), lr=cfg.learning_rate)

    if hasCuda: feature.cuda()

    encoder = Encoder(cfg)
    if cfg.model_type=='AC-RNN':
        e_opt = optim.SGD(ifilter(lambda p: p.requires_grad, encoder.parameters()), lr=cfg.actor_step_size)
    else:
        e_opt = optim.Adam(ifilter(lambda p: p.requires_grad, encoder.parameters()), lr=cfg.learning_rate)
    if hasCuda: encoder.cuda()

    if cfg.model_type=='INDP':
        indp = INDP(cfg)
        i_opt = optim.Adam(ifilter(lambda p: p.requires_grad, indp.parameters()), lr=cfg.learning_rate)
        if hasCuda: indp.cuda()

    elif cfg.model_type=='CRF':
        crf = CRF(cfg)
        c_opt = optim.Adam(ifilter(lambda p: p.requires_grad, crf.parameters()), lr=cfg.learning_rate)
        if hasCuda: crf.cuda()

    elif cfg.model_type=='TF-RNN':
        mldecoder = MLDecoder(cfg)
        m_opt = optim.Adam(ifilter(lambda p: p.requires_grad, mldecoder.parameters()), lr=cfg.learning_rate)
        if hasCuda: mldecoder.cuda()
        cfg.mldecoder_type = 'TF'

    elif cfg.model_type=='SS-RNN':
        mldecoder = MLDecoder(cfg)
        m_opt = optim.Adam(ifilter(lambda p: p.requires_grad, mldecoder.parameters()), lr=cfg.learning_rate)
        if hasCuda: mldecoder.cuda()
        cfg.mldecoder_type = 'SS'

    elif cfg.model_type=='AC-RNN':
        mldecoder = MLDecoder(cfg)
        m_opt = optim.SGD(ifilter(lambda p: p.requires_grad, mldecoder.parameters()), lr=cfg.actor_step_size)
        if hasCuda: mldecoder.cuda()
        cfg.mldecoder_type = 'TF'
        rltrain = RLTrain(cfg)
        r_opt = optim.Adam(ifilter(lambda p: p.requires_grad, rltrain.parameters()), lr=cfg.learning_rate, weight_decay=0.001)
        if hasCuda: rltrain.cuda()
        cfg.rltrain_type = 'AC'
        #For RL, the network should be pre-trained with teacher forced ML decoder.
        feature.load_state_dict(torch.load(path + 'TF-RNN' + '_feature'))
        encoder.load_state_dict(torch.load(path + 'TF-RNN' + '_encoder'))
        mldecoder.load_state_dict(torch.load(path + 'TF-RNN' + '_predictor'))

    if mode=='train':
        o_file = './temp.predicted_' + cfg.model_type
        best_val_cost = float('inf')
        best_val_epoch = 0
        first_start = time.time()
        epoch=0
        while (epoch < cfg.max_epochs):
            print
            print 'Model:{} | Epoch:{}'.format(cfg.model_type, epoch)

            if cfg.model_type=='SS-RNN':
                #Specify the decaying schedule for sampling probability.
                #inverse sigmoid schedule:
                cfg.sampling_p = float(cfg.k)/float(cfg.k + np.exp(float(epoch)/cfg.k))

            start = time.time()
            run_epoch(cfg)
            print '\nValidation:'
            predict(cfg, o_file)
            val_cost = 100 - evaluate(cfg, cfg.dev_ref, o_file)
            print 'Validation score:{}'.format(100 - val_cost)
            if val_cost < best_val_cost:
                best_val_cost = val_cost
                best_val_epoch = epoch
                torch.save(feature.state_dict(), path + cfg.model_type + '_feature')
                torch.save(encoder.state_dict(), path + cfg.model_type + '_encoder')
                if cfg.model_type=='INDP': torch.save(indp.state_dict(), path + cfg.model_type + '_predictor')
                elif cfg.model_type=='CRF': torch.save(crf.state_dict(), path + cfg.model_type + '_predictor')
                elif cfg.model_type=='TF-RNN' or cfg.model_type=='SS-RNN':
                    torch.save(mldecoder.state_dict(), path + cfg.model_type + '_predictor')
                elif cfg.model_type=='AC-RNN':
                    torch.save(mldecoder.state_dict(), path + cfg.model_type + '_predictor')
                    torch.save(rltrain.state_dict(), path + cfg.model_type + '_critic')

            #For early stopping
            if epoch - best_val_epoch > cfg.early_stopping:
                break
                ###

            print 'Epoch training time:{} seconds'.format(time.time() - start)
            epoch += 1

        print 'Total training time:{} seconds'.format(time.time() - first_start)

    elif mode=='test':
        cfg.batch_size = 256
        feature.load_state_dict(torch.load(path + cfg.model_type + '_feature'))
        encoder.load_state_dict(torch.load(path + cfg.model_type + '_encoder'))
        if cfg.model_type=='INDP': indp.load_state_dict(torch.load(path + cfg.model_type + '_predictor'))
        elif cfg.model_type=='CRF': crf.load_state_dict(torch.load(path + cfg.model_type + '_predictor'))
        elif cfg.model_type=='TF-RNN' or cfg.model_type=='SS-RNN':
            mldecoder.load_state_dict(torch.load(path + cfg.model_type + '_predictor'))
        elif cfg.model_type=='AC-RNN':
            mldecoder.load_state_dict(torch.load(path + cfg.model_type + '_predictor'))
            rltrain.load_state_dict(torch.load(path + cfg.model_type + '_critic'))

        print
        print 'Model:{} Predicting'.format(cfg.model_type)
        start = time.time()
        predict(cfg, o_file)
        print 'Total prediction time:{} seconds'.format(time.time() - start)
    return

"""
    For training: python tagger.py train <path to save model>
    example: python tagger.py train ./saved_models/

    For testing: python tagger.py test <path to restore model> <input file path> <output file path>
    example: python tagger.py test ./saved_models/ ./data/test.raw ./saved_models/test.predicted
    or: python tagger.py test ./saved_models/ ./data/dev.raw ./saved_models/dev.predicted
"""
if __name__ == "__main__":
    mode = sys.argv[1]
    path = sys.argv[2]
    in_file = None
    o_file = None
    if mode=='test':
        in_file = sys.argv[3]
        o_file = sys.argv[4]

    run_model(mode, path, in_file, o_file)
