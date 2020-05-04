#!/usr/bin/env python
# -*- coding: utf-8 -*-
# --------------------------------------------------------
# Descripttion: https://github.com/sxhxliang/detectron2_backbone
# version: 0.0.1
# Author: Shihua Liang (sxhx.liang@gmail.com)
# FilePath: /detectron2_backbone/detectron2_backbone/layers/wrappers.py
# Create: 2020-05-04 10:28:09
# LastAuthor: Shihua Liang
# lastTime: 2020-05-04 14:35:38
# --------------------------------------------------------
import math

import torch
from torch import nn
from torch.nn import init
from torch.nn import functional as F
from torch.nn.parameter import Parameter
from torch.nn.modules.utils import _single, _pair, _triple, _ntuple

TORCH_VERSION = tuple(int(x) for x in torch.__version__.split(".")[:2])


__all__ = ["_Conv2d", "Conv2d", "SeparableConv2d", "MaxPool2d"]

class _NewEmptyTensorOp(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, new_shape):
        ctx.shape = x.shape
        return x.new_empty(new_shape)

    @staticmethod
    def backward(ctx, grad):
        shape = ctx.shape
        return _NewEmptyTensorOp.apply(grad, shape), None


class _Conv2d(nn.Conv2d):
    def __init__(self, 
                in_channels, out_channels, kernel_size, 
                stride=1, padding=0, dilation=1, groups=1,
                bias=True, padding_mode='zeros', image_size=None):

        kernel_size = _pair(kernel_size)
        stride = _pair(stride)
        padding = _pair(padding)
        dilation = _pair(dilation)

        if padding_mode == 'static_same':
            p = max(kernel_size[0] - stride[0], 0)
            # tuple(pad_l, pad_r, pad_t, pad_b)
            padding = (p // 2, p - p // 2, p // 2, p - p // 2)
        elif padding_mode == 'dynamic_same':
            padding = _pair(0)
    
        super(_Conv2d, self).__init__(
            in_channels, out_channels, kernel_size, stride, padding, dilation, groups, bias, padding_mode)

    def conv2d_forward(self, input, weight):
        if self.padding_mode == 'circular':
            expanded_padding = ((self.padding[1] + 1) // 2, self.padding[1] // 2,
                                (self.padding[0] + 1) // 2, self.padding[0] // 2)
            input = F.pad(input, expanded_padding, mode='circular')

        if  self.padding_mode == 'dynamic_same':
            ih, iw = x.size()[-2:]
            kh, kw = self.weight.size()[-2:]
            sh, sw = self.stride
            oh, ow = math.ceil(ih / sh), math.ceil(iw / sw)
            pad_h = max((oh - 1) * self.stride[0] + (kh - 1) * self.dilation[0] + 1 - ih, 0)
            pad_w = max((ow - 1) * self.stride[1] + (kw - 1) * self.dilation[1] + 1 - iw, 0)
            if pad_h > 0 or pad_w > 0:
                # tuple(pad_l, pad_r, pad_t, pad_b)
                input = F.pad(input, [pad_w // 2, pad_w - pad_w // 2, pad_h // 2, pad_h - pad_h // 2])
        
        if  self.padding_mode == 'static_same':
                input = F.pad(input, self.padding)

        return F.conv2d(input, 
                        weight, self.bias, self.stride,
                        _pair(0), self.dilation, self.groups)

    def forward(self, input):
        return self.conv2d_forward(input, self.weight)


class Conv2d(_Conv2d):
    """
    A wrapper around :class:`torch.nn.Conv2d` to support empty inputs and more features.
    """

    def __init__(self, *args, **kwargs):
        """
        Extra keyword arguments supported in addition to those in `torch.nn.Conv2d`:

        Args:
            norm (nn.Module, optional): a normalization layer
            activation (callable(Tensor) -> Tensor): a callable activation function

        It assumes that norm layer is used before activation.
        """
        norm = kwargs.pop("norm", None)
        activation = kwargs.pop("activation", None)
        super().__init__(*args, **kwargs)

        self.norm = norm
        self.activation = activation

    def forward(self, x):
        if x.numel() == 0 and self.training:
            # https://github.com/pytorch/pytorch/issues/12013
            assert not isinstance(
                self.norm, torch.nn.SyncBatchNorm
            ), "SyncBatchNorm does not support empty inputs!"

        if x.numel() == 0 and TORCH_VERSION <= (1, 4):
            assert not isinstance(
                self.norm, torch.nn.GroupNorm
            ), "GroupNorm does not support empty inputs in PyTorch <=1.4!"
            # When input is empty, we want to return a empty tensor with "correct" shape,
            # So that the following operations will not panic
            # if they check for the shape of the tensor.
            # This computes the height and width of the output tensor
            output_shape = [
                (i + 2 * p - (di * (k - 1) + 1)) // s + 1
                for i, p, di, k, s in zip(
                    x.shape[-2:], self.padding, self.dilation, self.kernel_size, self.stride
                )
            ]
            output_shape = [x.shape[0], self.weight.shape[0]] + output_shape
            empty = _NewEmptyTensorOp.apply(x, output_shape)
            if self.training:
                # This is to make DDP happy.
                # DDP expects all workers to have gradient w.r.t the same set of parameters.
                _dummy = sum(x.view(-1)[0] for x in self.parameters()) * 0.0
                return empty + _dummy
            else:
                return empty

        x = super().forward(x)
        if self.norm is not None:
            x = self.norm(x)
        if self.activation is not None:
            x = self.activation(x)
        return x

class SeparableConv2d(_Conv2d):
    def __init__(self, in_channels, out_channels, kernel_size, 
                stride=1,padding=0, dilation=1, 
                bias=True, padding_mode='zeros', norm=None, activation=None):
        # depthwise_conv
        depthwise_bias = False
        depthwise_groups = in_channels
        super(SeparableConv2d, self).__init__(
                in_channels, in_channels, kernel_size, stride, padding, dilation, 
                depthwise_groups, depthwise_bias, padding_mode
            )
        # pointwise_conv
        point_kernel_size = _pair(1)
        self.out_channels = out_channels
        self.point_stride = _pair(1)
        self.point_padding = _pair(0)
        self.point_dilation = _pair(1)
        self.point_groups = 1

        self.weight2 = Parameter(torch.Tensor(
                out_channels, in_channels, *point_kernel_size))
        if bias:
            self.bias2 = Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter('bias2', None)
        self.reset_point_parameters()

        self.norm = norm
        self.activation = activation

    def reset_point_parameters(self):
        init.kaiming_uniform_(self.weight2, a=math.sqrt(5))
        if self.bias is not None:
            fan_in, _ = init._calculate_fan_in_and_fan_out(self.weight2)
            bound = 1 / math.sqrt(fan_in)
            init.uniform_(self.bias2, -bound, bound)
    
    def forward(self, input):
        # depthwise_conv
        x = self.conv2d_forward(input, self.weight)
        # pointwise_conv
        x = F.conv2d(x, self.weight2, self.bias2, self.point_stride,
                        self.point_padding, self.point_dilation, self.point_groups)

        if self.norm is not None:
            x = self.norm(x)
        if self.activation is not None:
            x = self.activation(x)
        return x

    def extra_repr(self):
        s = ('{in_channels}, {out_channels}, kernel_size={kernel_size}'
             ', stride={stride}')
        if self.padding != (0,) * len(self.padding):
            s += ', padding={padding}'
        if self.dilation != (1,) * len(self.dilation):
            s += ', dilation={dilation}'
        if self.output_padding != (0,) * len(self.output_padding):
            s += ', output_padding={output_padding}'
        if self.groups != 1:
            s += ', groups={groups}'
        if self.bias2 is None:
            s += ', bias=False'
        if self.padding_mode != 'zeros':
            s += ', padding_mode={padding_mode}'
        return s.format(**self.__dict__)


class MaxPool2d(nn.Module):
    def __init__(self, kernel_size, stride=None, padding=0, dilation=1, return_indices=False, ceil_mode=False, padding_mode='static_same'):
        super(MaxPool2d, self).__init__()
        self.kernel_size = _pair(kernel_size)
        self.stride = _pair(stride) or self.kernel_size
        self.padding = _pair(padding)
        self.dilation = _pair(dilation)
        self.return_indices = return_indices
        self.ceil_mode = ceil_mode
        self.padding_mode = padding_mode

        if padding_mode == 'static_same':
            p = max(self.kernel_size[0] - self.stride[0], 0)
            # tuple(pad_l, pad_r, pad_t, pad_b)
            padding = (p // 2, p - p // 2, p // 2, p - p // 2)
            self.padding = padding
        elif padding_mode == 'dynamic_same':
            padding = _pair(0)
            self.padding = padding

    def forward(self, input):
        input = F.pad(input, self.padding)
        return F.max_pool2d(input, self.kernel_size, self.stride,
                            _pair(0), self.dilation, self.ceil_mode,
                            self.return_indices)
    def extra_repr(self):
        return 'kernel_size={kernel_size}, stride={stride}, padding={padding}' \
            ', dilation={dilation}, ceil_mode={ceil_mode}, padding_mode={padding_mode}'.format(**self.__dict__)


