import math
import torch
from torch import Tensor
import torchvision
import torch.nn as nn
from typing import List

from common import ConfigTrainer
import common.utils as utils

from models.untils import BlockList, get_norm_layer
from models.seq_blocks import MlpNet
from models.mlp_features_embeding import MlpFeaturesEmedings
from models.layers import FcBlock, ConvBlock, ResBlock, SpatialAttentionBlock
from models.conv_unet import UNetConv
from models.torch_dct import Dct2DInverse, Dct2DForward


class PhaseRetrievalPredictor(nn.Module):
    def __init__(self,
                 config: ConfigTrainer,
                 out_ch: int,
                 out_img_size: int):
        super(PhaseRetrievalPredictor, self).__init__()
        self._config: ConfigTrainer = config
        self.out_img_size = out_img_size
        self.out_ch = out_ch

        if self._config.predict_out == 'features':
            if self._config.use_conv_block_predictor:
                if self._config.n_inter_features is None:
                    self.inter_ch = out_ch
                    if self._config.predict_type == 'spectral':
                        self.inter_ch *= 2
                else:
                    self.inter_ch = self._config.n_inter_features
            else:
                self.inter_ch = out_ch
        else:
            self.inter_ch = self._config.predict_img_int_features_multi_coeff * out_ch

        if self._config.predict_type == 'spectral':
            use_rfft = (self._config.use_rfft and not self._config.use_dct)
            self.inter_features_out_size = utils.get_magnitude_size_2d(out_img_size,
                                                                       self._config.add_pad_out,
                                                                       use_rfft=use_rfft)
        else:
            self.inter_features_out_size = [self.out_img_size, self.out_img_size]

        if self._config.use_dct and self._config.predict_type == 'spectral':
            assert self.inter_features_out_size[0] == self.inter_features_out_size[1]
            self._idct_features = Dct2DInverse(self.inter_features_out_size[0])
        else:
            self._idct_features = None

        self.input_mag_size_2d = utils.get_magnitude_size_2d(self._config.image_size, self._config.add_pad,
                                                             use_rfft=(self._config.use_rfft and not self._config.use_dct_input))

        # if self._config.use_dct and not self._config.use_dct_input:
        #     self.input_mag_size_2d[0] = self.input_mag_size_2d[1]

        self.in_features = self.input_mag_size_2d[0]*self.input_mag_size_2d[1]

        self.inter_features = self.inter_ch * self.inter_features_out_size[0] * self.inter_features_out_size[1]

        if self._config.deep_predict_fc is None:
            deep_fc = int(math.floor(math.log(self.inter_features / self.in_features,
                                              self._config.predict_fc_multy_coeff))) + 1
            deep_fc = max(3, deep_fc)
        else:
            deep_fc = self._config.deep_predict_fc

        self.out_features = self.out_ch * self.inter_features_out_size[0] * self.inter_features_out_size[1]

        if self._config.predict_type == 'spectral':
            if self._config.use_dct:
                out_fc_features = self.inter_features
            else:
                out_fc_features = 2 * self.inter_features
        elif self._config.predict_type == 'phase' or self._config.predict_type == 'special':
            out_fc_features = self.inter_features
        else:
            raise NameError(f'Not supported config.predict_type: {self._config.predict_type}')

        in_fc = self.in_features
        self.input_norm = get_norm_layer(name_type=self._config.magnitude_norm,
                                         input_ch=1,
                                         img_size=self.input_mag_size_2d,
                                         is_2d=True,
                                         affine=True)

        # self.fc_blocks = MlpNet(in_ch=in_fc,
        #                         deep=deep_fc,
        #                         out_ch=out_fc_features,
        #                         use_dropout=self._config.use_dropout_enc_fc,
        #                         multy_coeff=self._config.predict_fc_multy_coeff,
        #                         norm_type=self._config.norm_fc,
        #                         active_type=self._config.activation_fc_enc,
        #                         active_ch=self._config.activation_fc_ch_params)
        self.fc_blocks = BlockList()
        for ind in range(deep_fc):
            if ind == deep_fc - 1:
                out_fc = out_fc_features
            else:
                out_fc = int(math.floor(in_fc * self._config.predict_fc_multy_coeff))

            fc_block = FcBlock(in_fc, out_fc,
                               use_dropout=self._config.use_dropout_enc_fc,
                               norm_type=self._config.norm_fc,
                               active_type=self._config.activation_fc_enc,
                               active_params=out_fc if self._config.activation_fc_ch_params else 1)
            in_fc = out_fc

            self.fc_blocks.append(fc_block)

        self.weights_fc: List[nn.Parameter] = [fc_block.fc_seq[0].weight for fc_block in self.fc_blocks]
        self.inter_norm = get_norm_layer(name_type=self._config.inter_norm,
                                         input_ch=self.inter_ch,
                                         img_size=self.out_img_size,
                                         is_2d=True,
                                         affine=True)
        if self._config.use_conv_block_predictor:
            self.conv_blocks = self._build_conv_blocks()
        else:
            assert self.inter_ch == self.out_ch
            self.conv_blocks = None

    def _build_conv_blocks(self) -> nn.Module:
        conv_type = self._config.predict_conv_type
        active_type = self._config.activation_enc
        if conv_type == 'ConvBlock':
            conv_block_class = ConvBlock
        elif conv_type == 'ResBlock':
            conv_block_class = ResBlock
        elif conv_type == 'Unet':
            conv_block_class = UNetConv
        elif conv_type == 'SpatialAtt':
            assert self.inter_ch == self.out_ch, f'for SpatialAtt inter_ch: {self.inter_ch} != out_ch:{self.out_ch}'
        else:
            raise NameError(f'Non valid conv_type: {conv_type}')

        if conv_type == 'Unet':
            conv_blocks_module = UNetConv(img_ch=self.inter_ch,
                                          output_ch=self.out_ch,
                                          up_mode='bilinear',
                                          active_type=active_type,
                                          down_pool='avrg_pool',
                                          deep=self._config.deep_predict_conv)
        elif conv_type == 'SpatialAtt':
            conv_blocks_module = SpatialAttentionBlock(self.inter_ch, conv_type='conv_block', apply_att=True)
        else:
            in_ch = self.inter_ch
            out_ch = in_ch
            conv_blocks_module = BlockList()
            for ind in range(self._config.deep_predict_conv):
                out_ch = self._config.predict_conv_multy_coeff * in_ch
                conv_block = conv_block_class(in_ch, out_ch,
                                              active_type=active_type,
                                              norm_type=self._config.conv_pred_norm) # , padding_mode='zeros'
                in_ch = out_ch
                conv_blocks_module.append(conv_block)
            conv_out = nn.Conv2d(out_ch, self.out_ch, kernel_size=1, stride=1, padding=0)
            conv_blocks_module.append(conv_out)
            if self._config.spat_conv_predict:
                att_block = SpatialAttentionBlock(self.out_ch, conv_type='conv', apply_att=True)
                conv_blocks_module.append(att_block)
        return conv_blocks_module

    def extra_repr(self) -> str:
        return f'input_mag_size_2d={self.input_mag_size_2d[0]}x{self.input_mag_size_2d[1]}, ' \
               f'inter_shape={self.inter_ch}x{self.inter_features_out_size[0]}x{self.inter_features_out_size[1]}, '\
               f'out_shape={self.out_ch}x{self.out_img_size}x{self.out_img_size}'

    def forward(self, magnitude: Tensor) -> (Tensor, Tensor):
        magnitude = self.input_norm(magnitude)
        if self._config.predict_type == 'spectral' or self._config.predict_type == 'special':
            out_features, intermediate_features = self._special_pred(magnitude)
        elif self._config.predict_type == 'phase':
            out_features = self._angle_pred(magnitude)
            intermediate_features = out_features
            out_features = self.inter_norm(out_features.real)

            if self._config.features_sigmoid_active:
                out_features = torch.sigmoid(out_features)

        return out_features, intermediate_features

    def _angle_pred(self,  magnitude: Tensor) -> Tensor:
        magnitude = torch.fft.fftshift(magnitude, dim=(-2, -1))
        phase = self.conv_blocks(magnitude)
        exp_phase = torch.exp(torch.view_as_complex(torch.stack([torch.zeros_like(magnitude), phase], -1)))
        spectral = magnitude * exp_phase
        spectral = torch.fft.ifftshift(spectral, dim=(-2, -1))
        out_features = torch.fft.ifft2(spectral, norm=self._fft_norm)
        return out_features

    def _special_pred(self, magnitude: Tensor) -> (Tensor, Tensor):
        magnitude = torchvision.transforms.functional.crop(magnitude, 0, 0,
                                                           self.input_mag_size_2d[0],
                                                           self.input_mag_size_2d[1])

        mag_flatten = magnitude.view(-1, self.in_features)
        fc_features = self.fc_blocks(mag_flatten)
        if self._config.predict_type == 'spectral':
            if self._config.use_dct:
                spectral_features = fc_features.view(-1, self.inter_ch,
                                                     self.inter_features_out_size[0], self.inter_features_out_size[1])
                intermediate_features = self._idct_features(spectral_features)
            else:
                spectral_features = fc_features.view(-1, self.inter_ch,
                                                     self.inter_features_out_size[0], self.inter_features_out_size[1], 2)
                spectral_features = torch.view_as_complex(spectral_features)

                if self._config.use_rfft:
                    intermediate_features = torch.fft.irfft2(spectral_features,
                                                             (self.inter_features_out_size[0], self.inter_features_out_size[0]),
                                                             norm=self._config.fft_norm)
                else:
                    intermediate_features = torch.fft.ifft2(spectral_features, norm=self._config.fft_norm)
                    # out_features = intermediate_features.real
        elif self._config.predict_type == 'special':
            intermediate_features = fc_features.view(-1, self.inter_ch,
                                                     self.inter_features_out_size[0], self.inter_features_out_size[1])
        else:
            raise NameError(f'Not supported config.predict_type: {self._config.predict_type}')

        if self._config.add_pad_out:
            intermediate_features = torchvision.transforms.functional.center_crop(intermediate_features,
                                                                                  self.out_img_size)

        intermediate_features = (1/self._config.spectral_factor) * intermediate_features
        intermediate_features = self.inter_norm(intermediate_features)
        if self._config.features_sigmoid_active:
            intermediate_features = torch.sigmoid(intermediate_features)

        if self._config.use_conv_block_predictor:
            out_features = self.conv_blocks(intermediate_features)
        else:
            out_features = intermediate_features

        return out_features, intermediate_features




