from __future__ import print_function, division, absolute_import

import torch
import torch.nn as nn
import torch.nn.functional as F

# *******************************************************************************************************************
class SMOEScaleMap(nn.Module):
    r'''
        Compute SMOE Scale on a 4D tensor. This acts as a standard PyTorch layer. 
    
        Input should be:
        
        (1) A tensor of size [batch x channels x height x width] 
        (2) A tensor with only positive values. (After a ReLU)
        
        Output is a 3D tensor of size [batch x height x width] 
    '''
    def __init__(self, run_relu=False):
        
        super(SMOEScaleMap, self).__init__()
        
        r'''
            SMOE Scale must take in values > 0. Optionally, we can run a ReLU to do that.
        '''
        if run_relu:
            self.relu = nn.ReLU(inplace=False)
        else:
            self.relu = None
               
    def forward(self, x):

        assert(torch.is_tensor(x))
        assert(len(x.size()) > 2)
        
        
        r'''
            If we do not have a convenient ReLU to pluck from, we can do it here
        '''
        if self.relu is not None:
            x = self.relu(x)
                               
        r'''
            avoid log(0)
        '''
        x   = x + 0.0000001
        
        r'''
            This is one form. We can also use the log only form.
        '''
        m   = torch.mean(x,dim=1)
        k   = torch.log2(m) - torch.mean(torch.log2(x),dim=1)
        
        th  = k * m
        
        return th

# *******************************************************************************************************************
class StdMap(nn.Module):
    r'''
        Compute vanilla standard deviation on a 4D tensor. This acts as a standard PyTorch layer. 
    
        Input should be:
        
        (1) A tensor of size [batch x channels x height x width] 
        (2) Recommend a tensor with only positive values. (After a ReLU)
        
        Output is a 3D tensor of size [batch x height x width]
    '''
    def __init__(self):
        
        super(StdMap, self).__init__()
        
    def forward(self, x):
        
        assert(torch.is_tensor(x))
        assert(len(x.size()) > 2)
        
        x = torch.std(x,dim=1)
        
        return x
   
# *******************************************************************************************************************
class TruncNormalEntMap(nn.Module):
    r'''
        Compute truncated normal entropy on a 4D tensor. This acts as a standard PyTorch layer. 
    
        Input should be:
        
        (1) A tensor of size [batch x channels x height x width] 
        (2) This should come BEFORE a ReLU and can range over any real value
        
        Output is a 3D tensor of size [batch x height x width]
    '''
    def __init__(self):
        
        super(TruncNormalEntMap, self).__init__()
        
        self.c1 = torch.tensor(0.3989422804014327)  # 1.0/math.sqrt(2.0*math.pi)
        self.c2 = torch.tensor(1.4142135623730951)  # math.sqrt(2.0)
        self.c3 = torch.tensor(4.1327313541224930)  # math.sqrt(2.0*math.pi*math.exp(1))
    
    def _compute_alpha(self, mean, std, a=0):
        
        alpha = (a - mean)/std
        
        return alpha
        
    def _compute_pdf(self, eta):
        
        pdf = self.c1 * torch.exp(-0.5*eta.pow(2.0))
        
        return pdf
        
    def _compute_cdf(self, eta):
        
        e   = torch.erf(eta/self.c2)
        cdf = 0.5 * (1.0 + e)
        
        return cdf
    
    def forward(self, x):
        
        assert(torch.is_tensor(x))
        assert(len(x.size()) > 2)
 
        m   = torch.mean(x,   dim=1)
        s   = torch.std(x,    dim=1)
        a   = self._compute_alpha(m, s)
        pdf = self._compute_pdf(a)  
        cdf = self._compute_cdf(a) + 0.0000001  # Prevent log AND division by zero by adding a very small number
        Z   = 1.0 - cdf 
        T1  = torch.log(self.c3*s*Z)
        T2  = (a*pdf)/(2.0*Z)
        ent = T1 + T2

        return ent

# *******************************************************************************************************************                   
# ******************************************************************************************************************* 
class Normalize2D(nn.Module):
    r'''
        This will normalize a saliency map to range from 0 to 1 via normal cumulative distribution function. 
        
        Input and output will be a 3D tensor of size [batch size x height x width]. 
        
        Input can be any real valued number (supported by hardware)
        Output will range from 0 to 1
    '''
    
    def __init__(self):
        
        super(Normalize2D, self).__init__()   
        
    def forward(self, x):
        r'''
            Original shape is something like [64,7,7] i.e. [batch size x height x width]
        '''
        assert(torch.is_tensor(x))
        assert(len(x.size()) == 3) 
        
        s0      = x.size()[0]
        s1      = x.size()[1]
        s2      = x.size()[2]

        x       = x.reshape(s0,s1*s2) 
        
        m       = x.mean(dim=1)
        m       = m.reshape(m.size()[0],1)
        s       = x.std(dim=1)
        s       = s.reshape(s.size()[0],1)
        
        r'''
            The normal cumulative distribution function is used to squash the values from 0 to 1
        '''
        x       = 0.5*(1.0 + torch.erf((x-m)/(s*torch.sqrt(torch.tensor(2.0)))))
                
        x       = x.reshape(s0,s1,s2)
            
        return x   
     
# *******************************************************************************************************************     
# *******************************************************************************************************************
class CombineSaliencyMaps(nn.Module): 
    r'''
        This will combine saliency maps into a single weighted saliency map. 
        
        Input is a list of 3D tensors or various sizes. 
        Output is a 3D tensor of size output_size
        
        num_maps specifies how many maps we will combine
        weights is an optional list of weights for each layer e.g. [1, 2, 3, 4, 5]
    '''
    
    def __init__(self, output_size=[224,224], map_num=5, weights=None, resize_mode='bilinear',magnitude=False):
        
        super(CombineSaliencyMaps, self).__init__()
        
        assert(isinstance(output_size,list))
        assert(isinstance(map_num,int))
        assert(isinstance(resize_mode,str))    
        assert(len(output_size) == 2)
        assert(output_size[0] > 0)
        assert(output_size[1] > 0)
        assert(map_num > 0)
        
        r'''
            We support weights being None, a scaler or a list. 
            
            Depending on which one, we create a list or just point to one.
        '''
        if weights is None:
            self.weights = [1.0 for _ in range(map_num)]
        elif len(weights) == 1:
            assert(weights > 0)
            self.weights = [weights for _ in range(map_num)]   
        else:
            assert(len(weights) == map_num)        
            self.weights = weights
        
        self.weight_sum = 0
        
        for w in self.weights:
            self.weight_sum += w  
        
        self.map_num        = map_num
        self.output_size    = output_size
        self.resize_mode    = resize_mode
        self.magnitude      = magnitude
        
    def forward(self, smaps):
        
        r'''
            Input shapes are something like [64,7,7] i.e. [batch size x layer_height x layer_width]
            Output shape is something like [64,224,244] i.e. [batch size x image_height x image_width]
        '''

        assert(isinstance(smaps,list))
        assert(len(smaps) == self.map_num)
        assert(len(smaps[0].size()) == 3)   
        
        bn  = smaps[0].size()[0]
        cm  = torch.zeros((bn, 1, self.output_size[0], self.output_size[1]), dtype=smaps[0].dtype, device=smaps[0].device)
        ww  = []
        
        r'''
            Now get each saliency map and resize it. Then store it and also create a combined saliency map.
        '''
        if not self.magnitude:
            for i in range(len(smaps)):
                assert(torch.is_tensor(smaps[i]))
                wsz = smaps[i].size()
                w   = smaps[i].reshape(wsz[0], 1, wsz[1], wsz[2])
                w   = nn.functional.interpolate(w, size=self.output_size, mode=self.resize_mode, align_corners=False) 
                ww.append(w)
                cm  += (w * self.weights[i])
        else:
            for i in range(len(smaps)):
                assert(torch.is_tensor(smaps[i]))
                wsz = smaps[i].size()
                w   = smaps[i].reshape(wsz[0], 1, wsz[1], wsz[2])
                w   = nn.functional.interpolate(w, size=self.output_size, mode=self.resize_mode, align_corners=False) 
                w   = w*w 
                ww.append(w)
                cm  += (w * self.weights[i])
            
        cm  = cm / self.weight_sum
        cm  = cm.reshape(bn, self.output_size[0], self.output_size[1])
        
        ww  = torch.stack(ww,dim=1)
        ww  = ww.reshape(bn, self.map_num, self.output_size[0], self.output_size[1])
        
        return cm, ww 
        