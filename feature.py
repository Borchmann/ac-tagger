import torch
from torch.autograd import Variable
import torch.nn as nn
from torch.nn import init

hasCuda = torch.cuda.is_available()

class Feature(nn.Module):
    """
    Implements a character-level bi-direcitonl RNN
    to construct prefix/suffix patterns for each word.
    It then combines the capitalization and prefix/suffix patterns to
    word embeddings in order to build the final feature vectors.
    """
    def __init__(self, cfg):
        super(Feature, self).__init__()

        self.fw_ch_rnn = nn.GRUCell(
                            input_size=cfg.ch_em_size,
                            hidden_size=cfg.ch_rnn_units,
                            bias=True
                            )

        self.bw_ch_rnn = nn.GRUCell(
                            input_size=cfg.ch_em_size,
                            hidden_size=cfg.ch_rnn_units,
                            bias=True
                            )

        self.param_init()
        self.embeddings(cfg)
        return

    def param_init(self):
        for name, param in self.named_parameters():
            if 'bias' in name:
                init.constant(param, 0.0)
            if 'weight' in name:
                init.xavier_uniform(param)

        """
        Fills the input Tensor or Variable with a (semi) orthogonal matrix,
        as described in "Exact solutions to the nonlinear dynamics of learning
        in deep linear neural networks"
        """
        #Only for RNN weights
        for name, param in self.fw_ch_rnn.named_parameters():
            if 'weight' in name:
                init.orthogonal(param)

        for name, param in self.bw_ch_rnn.named_parameters():
            if 'weight' in name:
                init.orthogonal(param)

        return

    def embeddings(self, cfg):
        """Add embedding layer that maps from ids to vectors."""

        #We have 4 kinds of Capitalization patterns + 1 cap pad id.
        cfg.cap_em_size = 16
        self.cap_em = nn.Embedding(5, cfg.cap_em_size)
        self.cap_em.weight.data[cfg.cap_pad_id].fill_(0.0)
        self.cap_em.weight.requires_grad = True

        w_lt = torch.FloatTensor(cfg.data['w_v']) #word lookup table
        self.w_em = nn.Embedding(cfg.w_size, cfg.w_em_size)
        self.w_em.weight.data.copy_(w_lt)
        self.w_em.weight.data[cfg.w_pad_id].fill_(0.0)
        self.w_em.weight.requires_grad = True

        ch_lt = torch.FloatTensor(cfg.data['ch_v']) #char lookup table
        self.ch_em = nn.Embedding(cfg.ch_size, cfg.ch_em_size)
        self.ch_em.weight.data.copy_(ch_lt)
        self.ch_em.weight.data[cfg.ch_pad_id].fill_(0.0)
        self.ch_em.weight.requires_grad = True
        return

    def forward(self, cfg, B):
        #Tensor to Input Variables
        ch = Variable(B['ch'].cuda()) if hasCuda else Variable(B['ch'])
        rev_ch = Variable(B['rev_ch'].cuda()) if hasCuda else Variable(B['rev_ch'])
        w_len = Variable(B['w_len'].cuda()) if hasCuda else Variable(B['w_len'])
        w = Variable(B['w'].cuda()) if hasCuda else Variable(B['w'])
        w_cap = Variable(B['w_cap'].cuda()) if hasCuda else Variable(B['w_cap'])
        w_chs = Variable(B['w_chs'].cuda()) if hasCuda else Variable(B['w_chs'])
        w_mask = Variable(B['w_mask'].cuda()) if hasCuda else Variable(B['w_mask'])

        #Create a variable for initial hidden vector of RNNs.
        zeros = torch.zeros(ch.size()[0], cfg.ch_rnn_units)
        h0 = Variable(zeros.cuda()) if hasCuda else Variable(zeros)

        #Forward RNN to build suffix
        ch_ems = self.ch_em(ch).permute(1, 0, 2)
        fw_outputs = []
        for i in range(cfg.max_w_len):
            if i==0: fwh = h0
            fwh = self.fw_ch_rnn(ch_ems[i], fwh)
            fw_outputs.append(fwh)

        fw_ch_h = torch.stack(fw_outputs, dim=1)

        #l_index for last valid time step.
        l_index = (w_len - 1).view(-1, 1, 1).expand(-1, 1, cfg.ch_rnn_units)
        suffix_ems = torch.gather(fw_ch_h, 1, l_index).view(-1, cfg.ch_rnn_units)
        S = torch.index_select(suffix_ems, 0, w_chs.view(-1,))
        Suffixes = S.view(cfg.d_batch_size, cfg.max_s_len, cfg.ch_rnn_units)


        #Backward RNN to build prefix
        rev_ch_ems = self.ch_em(rev_ch).permute(1, 0, 2)
        bw_outputs = []
        for i in range(cfg.max_w_len):
            if i==0: bwh = h0
            bwh = self.bw_ch_rnn(rev_ch_ems[i], bwh)
            bw_outputs.append(bwh)


        bw_ch_h = torch.stack(bw_outputs, dim=1)
        prefix_ems = torch.gather(bw_ch_h, 1, l_index).view(-1, cfg.ch_rnn_units)
        P = torch.index_select(prefix_ems, 0, w_chs.view(-1,))
        Prefixes = P.view(cfg.d_batch_size, cfg.max_s_len, cfg.ch_rnn_units)

        mask = w_mask.view(cfg.d_batch_size, cfg.max_s_len, 1)
        mask_expanded = mask.expand(cfg.d_batch_size, cfg.max_s_len, cfg.ch_rnn_units)

        Suffixes_masked = Suffixes * mask_expanded
        Prefixes_masked = Prefixes * mask_expanded
        Words = self.w_em(w)
        Caps = self.cap_em(w_cap)

        features = torch.cat((Prefixes_masked, Words, Suffixes_masked, Caps), 2)
        return features
