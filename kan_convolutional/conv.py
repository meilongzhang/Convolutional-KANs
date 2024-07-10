from spconv.pytorch.conv import SparseConvolution
from spconv.pytorch import ops
from spconv.pytorch.core import expand_nd
from spconv.core import ConvAlgo
from spconv.pytorch.cppcore import TorchAllocator
import spconv.pytorch.functional as Fsp
from cumm import tensorview as tv
from torch.nn.parameter import Parameter
import math
import spconv.pytorch as spconv
from spconv.pytorch import functional as Fsp
from torch import nn
from spconv.pytorch.utils import PointToVoxel
from spconv.pytorch.hash import HashTable
from spconv.pytorch.utils import PointToVoxel, gather_features_by_pc_voxel_id
import numpy as np
import torch.nn.functional as F
import torch
from numba import jit
array = np.array
float32 = np.float32


class SparseKANConv3D(torch.nn.Module):
      """
      A pure Pytorch version of SparseKANConv3D. Offers Sparse 3D Convolution with Kolmogorov-Arnold Networks
      """

      def __init__(self,
                   ndim: int,
                   in_channels: int,
                   out_channels: int,
                   kernel_size=3,
                   stride=1,
                   padding=0,
                   dilation=1,
                   groups=1,
                   bias: bool = False,
                   subm: bool = False,
                   output_padding=0,
                   transposed: bool = False,
                   grid_size=5,
                   spline_order=3,
                   grid_range=[-1, 1],
                   grid_eps=0.02,
                   base_activation = torch.nn.SiLU,
                   device='cpu'):
            super(SparseKANConv3D, self).__init__()
            self.in_channels = in_channels
            self.out_channels = out_channels

            self.kernel_size = expand_nd(ndim, kernel_size)
            self.stride = expand_nd(ndim, stride)
            self.padding = expand_nd(ndim, padding)
            self.dilation = expand_nd(ndim, dilation)
            self.output_padding = expand_nd(ndim, output_padding)
            self.num_kernel_elems = math.prod(self.kernel_size)

            self.subm = subm
            self.transposed = transposed
            self.device = device

            self.grid_size = grid_size
            self.spline_order = spline_order
            self.base_activation = base_activation()
            self.grid_eps = grid_eps

            num_elements = self.num_kernel_elems * in_channels
            h = (grid_range[1] - grid_range[0]) / grid_size

            # this grid is shared resource for all kernel elements
            self.grid = (
                (
                    torch.arange(-spline_order, grid_size + spline_order + 1) * h + grid_range[0]
                )
                .expand(num_elements, -1)
                .contiguous()
            ).reshape((self.num_kernel_elems,in_channels,-1)).to(device) # 27 kernel locations, 3 input channels, 5 kernels (output channels), 12 bspline parameters
            #self.register_buffer("grid", self.grid)
            self.base_weights = torch.nn.Parameter(torch.Tensor(self.num_kernel_elems, out_channels, in_channels)).to(device)
            self.spline_weights = torch.nn.Parameter(torch.Tensor(self.num_kernel_elems, out_channels, in_channels * (grid_size + spline_order))).to(device)

      def curve2coeff(self, x: torch.Tensor, y: torch.Tensor, kernel_idx):
            #print(x.shape)
            A = self.b_splines(x, kernel_idx).transpose(0, 1)  # (in_features, batch_size, grid_size + spline_order)
            #print('A', A.shape)
            B = y.transpose(0, 1)
            solution = torch.linalg.lstsq(A, B).solution
            result = solution.permute(2, 0, 1)
            return result.reshape(self.out_channels, -1).contiguous()

      @torch.no_grad()
      def update_grid(self, x: torch.Tensor, margin=0.01, kernel_idx=0):
            batch = x.size(0)

            splines = self.b_splines(x, kernel_idx)#.unsqueeze(0)  (batch, in, coeff)
            splines = splines.permute(1, 0, 2)  # (in, batch, coeff)
            orig_coeff = self.spline_weights[kernel_idx].view(self.out_channels, self.in_channels, -1) #self.scaled_spline_weight  # (out, in, coeff)
            orig_coeff = orig_coeff.permute(1, 2, 0)  # (in, coeff, out)
            unreduced_spline_output = torch.bmm(splines, orig_coeff)  # (in, batch, out)
            unreduced_spline_output = unreduced_spline_output.permute(
                1, 0, 2
            )  # (batch, in, out)
            x_sorted = torch.sort(x, dim=0)[0]#.view(1, 3)
            grid_adaptive = x_sorted[
                torch.linspace(
                    0, batch-1, self.grid_size + 1, dtype=torch.int64, device=x.device
                )
            ]
            uniform_step = (x_sorted[-1] - x_sorted[0] + 2 * margin) / self.grid_size
            grid_uniform = (
                torch.arange(
                    self.grid_size + 1, dtype=torch.float32, device=x.device
                ).unsqueeze(1)
                * uniform_step
                + x_sorted[0]
                - margin
            )
            new_grid = self.grid_eps * grid_uniform + (1 - self.grid_eps) * grid_adaptive
            new_grid = torch.concatenate(
                [
                    new_grid[:1]
                    - uniform_step
                    * torch.arange(self.spline_order, 0, -1, device=x.device).unsqueeze(1),
                    new_grid,
                    new_grid[-1:]
                    + uniform_step
                    * torch.arange(1, self.spline_order + 1, device=x.device).unsqueeze(1),
                ],
                dim=0,
            )
            self.grid[kernel_idx].copy_(new_grid.T)
            self.spline_weights[kernel_idx].data.copy_(self.curve2coeff(x, unreduced_spline_output, kernel_idx))


      def b_splines(self, x: torch.Tensor, kernel_idx):
            x = x.unsqueeze(-1)
            bases = ((x >= self.grid[kernel_idx][:,:-1]) & (x < self.grid[kernel_idx][:,1:])).to(torch.float32)
            for k in range(1, self.spline_order + 1):
                bases = (
                    (x - self.grid[kernel_idx][:,:-(k+1)])
                    / (self.grid[kernel_idx][:,k:-1] - self.grid[kernel_idx][:,:-(k+1)])
                    * bases[:,:,:-1]
                ) + (
                    (self.grid[kernel_idx][:,k+1:] - x)
                    / (self.grid[kernel_idx][:,k+1:] - self.grid[kernel_idx][:,1:(-k)])
                    * bases[:,:,1:]
                )
            return bases.contiguous()


      def forward(self, x: spconv.SparseConvTensor):
            ## Currently supporting only sparseconv tensors

            ## Calculate input output pairs
            outids, indice_pairs, indice_pair_num = ops.get_indice_pairs(x.indices,
                                                                         x.batch_size,
                                                                         x.spatial_shape,
                                                                         ConvAlgo.Native,
                                                                         self.kernel_size,
                                                                         self.stride,
                                                                         self.padding,
                                                                         self.dilation,
                                                                         self.output_padding,
                                                                         self.subm,
                                                                         self.transposed)

            ## Copy and calculate some sparse tensor attributes
            out_tensor = x.shadow_copy()
            out_spatial_shape = ops.get_conv_output_size(
                    x.spatial_shape, self.kernel_size, self.stride, self.padding, self.dilation)
            indice_dict = x.indice_dict.copy()
            out_features = torch.zeros((outids.size(0), self.out_channels), device=self.device)

            ## Do the actual convolution
            ## Proxy convolution Function
            for kernel_idx in range(27):
                ### DO THIS IN PARALLEL PER KERNEL ELEMENT ###
                iopairs = indice_pairs[:,kernel_idx,:indice_pair_num[kernel_idx]] # all the input-output pairs for kernel

                inp = iopairs[0, :]
                out = iopairs[1, :]
                x = features[inp]#[:, :, None]
                self.update_grid(x, margin=0.01, kernel_idx=kernel_idx)
                bases = self.b_splines(x, kernel_idx)
                #print(bases.shape)
                out_features[out] += (
                    F.linear(bases.view(-1, bases.size(-1)*bases.size(-2)), self.spline_weights[kernel_idx]).squeeze(0) +
                    F.linear(self.base_activation(x), self.base_weights[kernel_idx])
                ).squeeze(0)



                #for i in range(len(reee[0])): # do this for all valid input-output pairs of kernel element
                    #inp, out = iopairs[:,i] # I have here the input and output index of the pair

                    # fetch features associated with the input index
                    #x = features[inp][:,None]

                    ### UPDATE GRID
                    #self.update_grid(x, margin=0.01, kernel_idx=kernel_idx)
            """
                    x_sorted = torch.sort(x, dim=0)[0].view(1, 3)
                    grid_adaptive = x_sorted[
                        torch.linspace(
                            0, 0, grid_size + 1, dtype=torch.int64, device=x.device
                        )
                    ]

                    uniform_step = (x_sorted[-1] - x_sorted[0] + 2 * margin) / grid_size
                    grid_uniform = (
                        torch.arange(
                            grid_size + 1, dtype=torch.float32, device=x.device
                        ).unsqueeze(1)
                        * uniform_step
                        + x_sorted[0]
                        - margin
                    )

                    new_grid = grid_eps * grid_uniform + (1 - grid_eps) * grid_adaptive
                    new_grid = torch.concatenate(
                        [
                            new_grid[:1]
                            - uniform_step
                            * torch.arange(spline_order, 0, -1, device=x.device).unsqueeze(1),
                            new_grid,
                            new_grid[-1:]
                            + uniform_step
                            * torch.arange(1, spline_order + 1, device=x.device).unsqueeze(1),
                        ],
                        dim=0,
                    )

                    grid[kernel_idx].copy_(new_grid.T)
                    """
                    ### FINISH GRID UPDATE
                    #bases = self.b_splines(x, kernel_idx)

            """
                    bases = ((x >= grid[kernel_idx][:,:-1]) & (x < grid[kernel_idx][:,1:])).to(torch.float32)
                    for k in range(1, spline_order + 1):
                        bases = (
                            (x - grid[kernel_idx][:,:-(k+1)])
                            / (grid[kernel_idx][:,k:-1] - grid[kernel_idx][:,:-(k+1)])
                            * bases[:,:-1]
                        ) + (
                            (grid[kernel_idx][:,k+1:] - x)
                            / (grid[kernel_idx][:,k+1:] - grid[kernel_idx][:,1:(-k)])
                            * bases[:,1:]
                        )
                    """
                    
                    #out_features[out] += (F.linear(bases.view(1, -1), spline_weights[kernel_idx]).squeeze(0) + F.linear(self.base_activation(x.T), self.base_weights[kernel_idx]))[0]

            out_tensor = out_tensor.replace_feature(out_features)
            out_tensor.indices = outids
            out_tensor.indice_dict = indice_dict
            out_tensor.spatial_shape = out_spatial_shape
            return out_tensor
      
if __name__ == '__main__':
    # Test SparseKANConv3D
    # Create a SparseConvTensor
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    gen = PointToVoxel(
        vsize_xyz=[0.1, 0.1, 0.1],
        coors_range_xyz=[-80, -80, -2, 80, 80, 6],
        num_point_features=3,
        max_num_voxels=5000,
        max_num_points_per_voxel=5)
    pc = np.random.uniform(-10, 10, size=[1000, 3])
    pc_th = torch.from_numpy(pc)
    voxels, coords, num_points_per_voxel = gen(pc_th, empty_mean=True)

    indices = torch.cat((torch.zeros(voxels.shape[0], 1), coords[:, [2,1,0]]), dim=1).to(torch.int32)
    features = torch.max(voxels, dim=1)[0]
    spatial_shape = [1600, 1600, 80]
    batch_size = 1
    features = features.to(device)
    indices = indices.to(device)

    test_sparse = spconv.SparseConvTensor(features, indices, spatial_shape, batch_size)
    # Create a SparseKANConv3D
    kan_conv = SparseKANConv3D(3, 3, 5, device=device)
    # Perform a forward pass
    out = kan_conv(test_sparse)
    # Check if the output is correct