import sys
import numpy as np
import torch
import torch.nn as nn
from time import time
from utils.layout import ch_locations_2d
import torch.nn.functional as F
from constants import device


class SpatialAttentionOrig(nn.Module):
    """This is easier to understand but very slow. I reimplemented to SpatialAttention"""

    def __init__(self, args, z_re=None, z_im=None):
        super(SpatialAttentionOrig, self).__init__()

        self.D1 = args.D1
        self.K = args.K

        if z_re is None or z_im is None:
            self.z_re = nn.Parameter(torch.Tensor(self.D1, self.K, self.K))
            self.z_im = nn.Parameter(torch.Tensor(self.D1, self.K, self.K))
            nn.init.kaiming_uniform_(self.z_re, a=np.sqrt(5))
            nn.init.kaiming_uniform_(self.z_im, a=np.sqrt(5))
        else:
            self.z_re = z_re
            self.z_im = z_im

        self.ch_locations_2d = ch_locations_2d(args.dataset).cuda()

    def fourier_space(self, j, x: torch.Tensor, y: torch.Tensor):  # x: ( 60, ) y: ( 60, )
        a_j = 0
        for k in range(self.K):
            for l in range(self.K):
                a_j += self.z_re[j, k, l] * torch.cos(2 * torch.pi * (k * x + l * y))
                a_j += self.z_im[j, k, l] * torch.sin(2 * torch.pi * (k * x + l * y))

        return a_j  # ( 60, )

    def forward(self, X):  # ( B, C, T ) (=( 128, 60, 256 ))
        spat_attn = []
        loc = self.ch_locations_2d  # ( 60, 2 )
        for j in range(self.D1):
            a_j = self.fourier_space(j, loc[:, 0], loc[:, 1])  # ( 60, )

            # sa.append(torch.exp(a_j) @ X / torch.exp(a_j).sum()) # ( 128, 256 )
            spat_attn.append(torch.einsum('c,bct->bt', torch.exp(a_j), X) / torch.exp(a_j).sum())  # ( 128, 256 )

        spat_attn = torch.stack(spat_attn)  # ( 270, 128, 256 )

        return spat_attn.permute(1, 0, 2)  # ( 128, 270, 256 )


class SpatialAttention(nn.Module):
    """Faster version of SpatialAttentionOrig"""

    def __init__(self, args):
        super(SpatialAttention, self).__init__()

        self.D1 = args.D1
        self.K = args.K

        self.z_re = nn.Parameter(torch.Tensor(self.D1, self.K, self.K))
        self.z_im = nn.Parameter(torch.Tensor(self.D1, self.K, self.K))
        nn.init.kaiming_uniform_(self.z_re, a=np.sqrt(5))
        nn.init.kaiming_uniform_(self.z_im, a=np.sqrt(5))

        self.K_arange = torch.arange(self.K).cuda()

        self.ch_locations_2d = ch_locations_2d(args.dataset).cuda()

    def fourier_space(self, x: torch.Tensor, y: torch.Tensor):  # x: ( 60, ) y: ( 60, )

        rad1 = torch.einsum('k,c->kc', self.K_arange, x)
        rad2 = torch.einsum('l,c->lc', self.K_arange, y)
        # rad = torch.einsum('kc,lc->kcl', rad1, rad2)

        # ( 32, 1, 60 ) + ( 1, 32, 60 ) -> ( 32, 32, 60 )
        rad = rad1.unsqueeze(1) + rad2.unsqueeze(0)

        real = torch.einsum('dkl,klc->dc', self.z_re, torch.cos(2 * torch.pi * rad))  # ( 270, 60 )
        imag = torch.einsum('dkl,klc->dc', self.z_im, torch.sin(2 * torch.pi * rad))

        return real + imag  # ( 270, 60 )

    def fourier_space_orig(self, x: torch.Tensor, y: torch.Tensor):  # x: ( 60, ) y: ( 60, )
        """Slower version of fourier_space"""

        a = torch.zeros(self.D1, x.shape[0], device='cuda')  # ( 270, 60 )
        for k in range(self.K):
            for l in range(self.K):
                # This einsum is same as torch.stack([_d * c for _d in d])
                a += torch.einsum('d,c->dc', self.z_re[:, k, l],
                                  torch.cos(2 * torch.pi * (k * x + l * y)))  # ( 270, 60 )
                a += torch.einsum('d,c->dc', self.z_im[:, k, l], torch.sin(2 * torch.pi * (k * x + l * y)))

        return a  # ( 270, 60 )

    def forward(self, X):  # ( 128, 60, 256 )
        loc = self.ch_locations_2d  # ( 60, 2 )

        a = self.fourier_space(loc[:, 0], loc[:, 1])  # ( 270, 60 )
        # _a = self.fourier_space_orig(loc[:,0], loc[:,1]) # ( 270, 60 )
        # print(torch.equal(_a, a))

        # ( 270, 60 ) @ ( 128, 60, 256 ) -> ( 128, 256, 270 )
        spat_attn = torch.einsum('dc,bct->btd', torch.exp(a), X) / torch.exp(a).sum(dim=1)

        return spat_attn.permute(0, 2, 1)  # ( 128, 270, 256 )


class SpatialAttentionX(nn.Module):
    """Same as SpatialAttention, but a little more concise"""

    def __init__(self, args):
        super(SpatialAttentionX, self).__init__()

        self.spatial_dropout = SpatialDropout(args)

        # vectorize of k's and l's
        a = []
        for k in range(args.K):
            for l in range(args.K):
                a.append((k, l))
        a = torch.tensor(a)
        k, l = a[:, 0], a[:, 1]

        # vectorize x- and y-positions of the sensors
        loc = ch_locations_2d(args.dataset)
        x, y = loc[:, 0], loc[:, 1]

        # make a complex-valued parameter, reshape k,l into one dimension
        self.z = nn.Parameter(torch.rand(size=(args.D1, args.K**2), dtype=torch.cfloat)).to(device)

        # NOTE: pre-compute the values of cos and sin (they depend on k, l, x and y which repeat)
        phi = 2 * torch.pi * (torch.einsum('k,x->kx', k, x) + torch.einsum('l,y->ly', l, y))  # torch.Size([1024, 60]))
        self.cos = torch.cos(phi).to(device)
        self.sin = torch.cos(phi).to(device)

    def forward(self, X):

        # NOTE: do hadamard product and and sum over l and m (i.e. m, which is l X m)
        re = torch.einsum('jm, me -> je', self.z.real, self.cos)  # torch.Size([270, 60])
        im = torch.einsum('jm, me -> je', self.z.imag, self.sin)
        a = re + im  # essentially (unnormalized) weights with which to mix input channels into ouput channels

        # NOTE: to get the softmax spatial attention weights over input electrodes,
        # we don't compute exp, etc (as in the eq. 5), we take softmax instead:
        SA_wts = F.softmax(a, dim=-1)  # each row sums to 1

        # NOTE: drop some channels within a d_drop of the sampled channel
        dropped_X = self.spatial_dropout(X)

        # NOTE: each output is a diff weighted sum over each input channel
        return torch.einsum('oi,bit->bot', SA_wts, dropped_X)


class SpatialDropout(nn.Module):
    # NOTE: in progress
    # FIXME: now each item in a batch gets the same channels masked

    def __init__(self, args):
        super(SpatialDropout, self).__init__()
        self.p = args.p_spatial_drop
        self.d_drop = args.d_drop

        loc = ch_locations_2d(args.dataset)
        self.loc = [loc[i, :].flatten() for i in range(loc.shape[0])]

    def get_dropouts(self):
        drop_id = np.random.choice(len(self.loc))
        drop_center = self.loc[drop_id]
        dropouts = []
        for i, coord in enumerate(self.loc):
            if (coord - drop_center).norm() < self.d_drop:
                dropouts.append(i)
        return dropouts

    def forward(self, X):
        dropouts = self.get_dropouts()
        X[:, dropouts, :] = 0.0
        return X


class SubjectBlock(nn.Module):

    # args
    def __init__(self, args):
        super(SubjectBlock, self).__init__()

        self.num_subjects = args.num_subjects
        self.D1 = args.D1
        self.K = args.K
        self.spatial_attention = SpatialAttentionX(args)
        # self.spatial_attention = SpatialAttention(args)
        # self.spatial_attention = SpatialAttentionOrig()
        self.conv = nn.Conv1d(in_channels=self.D1, out_channels=self.D1, kernel_size=1, stride=1)
        self.subject_matrix = nn.Parameter(torch.rand(self.num_subjects, self.D1, self.D1))
        self.subject_layer = [
            nn.Conv1d(in_channels=self.D1, out_channels=self.D1, kernel_size=1, stride=1, device='cuda')
            for _ in range(self.num_subjects)
        ]

    def forward(self, X, subject_idxs):
        X = self.spatial_attention(X)  # ( B, 270, 256 )
        X = self.conv(X)  # ( B, 270, 256 )

        # X = self.subject_matrix[s] @ X # ( 270, 270 ) @ ( B , 270, 256 ) -> ( B, 270, 256 )
        # TODO make this more efficient
        _X = []
        for i, x in enumerate(X):  # x: ( 270, 256 )
            x = self.subject_layer[subject_idxs[i]](x.unsqueeze(0))  # ( 1, 270, 256 )
            _X.append(x.squeeze())

        X = torch.stack(_X)

        return X  # ( B, 270, 256 )


class ConvBlock(nn.Module):

    def __init__(self, k, D1, D2):
        super(ConvBlock, self).__init__()

        self.k = k
        self.D2 = D2
        self.in_channels = D1 if k == 1 else D2

        self.conv1 = nn.Conv1d(
            in_channels=self.in_channels,
            out_channels=self.D2,
            kernel_size=3,
            padding='same',
            dilation=2**(2 * self.k % 5),
        )
        self.batchnorm1 = nn.BatchNorm1d(num_features=self.D2)
        self.conv2 = nn.Conv1d(
            in_channels=self.D2,
            out_channels=self.D2,
            kernel_size=3,
            padding='same',
            dilation=2**(2 * self.k + 1 % 5),
        )
        self.batchnorm2 = nn.BatchNorm1d(num_features=self.D2)
        self.conv3 = nn.Conv1d(
            in_channels=self.D2,
            out_channels=2 * self.D2,
            kernel_size=3,
            padding='same',
            dilation=2,
        )

    def forward(self, X):
        if self.k == 1:
            X = self.conv1(X)
        else:
            X = self.conv1(X) + X  # skip connection
        X = nn.GELU()(self.batchnorm1(X))

        X = self.conv2(X) + X  # skip connection
        X = nn.GELU()(self.batchnorm2(X))

        X = self.conv3(X)
        X = nn.GLU(dim=-2)(X)

        return X  # ( B, 320, 256 )


class BrainEncoder(nn.Module):

    def __init__(self, args):
        super(BrainEncoder, self).__init__()

        self.num_subjects = args.num_subjects
        self.D1 = args.D1
        self.D2 = args.D2
        self.F = args.F if not args.last4layers else 1024
        self.K = args.K
        self.dataset_name = args.dataset

        self.subject_block = SubjectBlock(args)

        self.conv_blocks = nn.Sequential()
        for k in range(1, 6):
            self.conv_blocks.add_module(f"conv{k}", ConvBlock(k, self.D1, self.D2))

        self.conv_final1 = nn.Conv1d(
            in_channels=self.D2,
            out_channels=2 * self.D2,
            kernel_size=1,
        )
        self.conv_final2 = nn.Conv1d(
            in_channels=2 * self.D2,
            out_channels=self.F,
            kernel_size=1,
        )

    def forward(self, X, subject_idxs):
        X = self.subject_block(X, subject_idxs)
        X = self.conv_blocks(X)
        # print(X.shape)
        X = nn.GELU()(self.conv_final1(X))
        # print(X.shape)
        X = nn.GELU()(self.conv_final2(X))

        return X


if __name__ == '__main__':
    # torch.autograd.set_detect_anomaly(True)

    brain_encoder = BrainEncoder().cuda()
    # brain_encoder = SpatialAttention().cuda()
    # brain_encoder = SubjectBlock().cuda()
    # brain_encoder_ = SpatialAttentionOrig(
    #     brain_encoder.z_re.clone(), brain_encoder.z_im.clone()
    # ).cuda()

    X = torch.rand(128, 60, 256).cuda()
    X.requires_grad = True

    subject_idxs = torch.randint(19, size=(128,))

    Z = brain_encoder(X, subject_idxs)  # ( 512, 270, 256 )

    # Z_ = brain_encoder_(X)

    # print(torch.equal(Z, Z_))

    # print((Z - Z_).sum())

    stime = time()
    grad = torch.autograd.grad(outputs=Z,
                               inputs=X,
                               grad_outputs=torch.ones_like(Z),
                               create_graph=True,
                               retain_graph=True,
                               only_inputs=True)[0]
    print(f"grad {time() - stime}")