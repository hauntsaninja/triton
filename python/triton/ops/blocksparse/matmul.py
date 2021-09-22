import triton
import triton.language as tl
import triton._C.libtriton as libtriton
import torch

# ********************************************************
# --------------------------------------------------------
# Sparse = Dense x Dense (SDD)
# This operation uses super-blocking to make sure that
# it's done efficiently when small blocks can be grouped
# together
# --------------------------------------------------------
# ********************************************************

@triton.jit
def _sdd_kernel(
    A, B, C, 
    stride_za, stride_ha, stride_ma, stride_ak, 
    stride_zb, stride_hb, stride_bk, stride_nb, 
    stride_zc, stride_mc, stride_nc, 
    K, grid_offset, lut, **meta
):
    TILE_M = meta['TILE_M']
    TILE_N = meta['TILE_N']
    TILE_K = meta['TILE_K']
    BLOCK  = meta['BLOCK']
    #------------#
    #- Prologue -#
    #------------#
    pid1 = tl.program_id(1) + grid_offset
    blockidm = tl.arange(0, TILE_M) // BLOCK
    blockidn = tl.arange(0, TILE_N) // BLOCK
    offlutm = blockidm * (TILE_N // BLOCK) * 4
    offlutn = blockidn * 4
    header = lut + pid1 * (TILE_M // BLOCK) * (TILE_N // BLOCK) * 4
    # batch offset
    off_z = tl.program_id(2)
    # head offset
    off_h = tl.load(header + 0)
    # initialize pointers to A
    start_am = tl.load(header + 1 + offlutm)
    offs_am = start_am * BLOCK + (tl.arange(0, TILE_M) % BLOCK)
    offs_ak = tl.arange(0, TILE_K)
    a_ptrs = A + off_z * stride_za \
                + off_h * stride_ha \
                + offs_am[:, None] * stride_ma \
                + offs_ak[None, :] * stride_ak
    # initialize pointers to B
    start_bn = tl.load(header + 2 + offlutn)
    offs_bn = start_bn * BLOCK + (tl.arange(0, TILE_N) % BLOCK)
    offs_bk = tl.arange(0, TILE_K)
    b_ptrs = B + off_z * stride_zb \
                + off_h * stride_hb \
                + offs_bn[None, :] * stride_nb \
                + offs_bk[:, None] * stride_bk
    ## ---------------- ##
    ##    Inner Loop    ##
    ## ---------------- ##
    acc = tl.zeros((TILE_M, TILE_N), dtype=tl.float32)
    for k in range(K, 0, -TILE_K):
        a = tl.load(a_ptrs)
        b = tl.load(b_ptrs)
        acc += tl.dot(a, b)
        a_ptrs += TILE_K * stride_ak
        b_ptrs += TILE_K * stride_bk
    c = acc.to(C.dtype.element_ty)
    ## ---------------- ##
    ##    Epilogue      ##
    ## ---------------- ##
    blockidm = tl.arange(0, TILE_M) // BLOCK
    blockidn = tl.arange(0, TILE_N) // BLOCK
    offlutm = blockidm * (TILE_N // BLOCK) * 4
    offlutn = blockidn * 4
    off_block_id = 3 + offlutm[:, None] + offlutn[None, :]
    block_id = tl.load(header + off_block_id)
    off_c = block_id * BLOCK * BLOCK
    # initialize pointers to C
    offs_cm = tl.arange(0, TILE_M) % BLOCK
    offs_cn = tl.arange(0, TILE_N) % BLOCK
    pc = C + off_c \
            + off_z * stride_zc \
            + offs_cm[:, None] * stride_mc \
            + offs_cn[None, :] * stride_nc
    tl.store(pc, c, mask=True)

def sdd_matmul(a, b, trans_a, trans_b, trans_c, spdims, block, luts, num_locks, widths, packs):
    # (A * B)^T = B^T * A^T
    if trans_c:
        a, b = b, a
        trans_a, trans_b = not trans_b, not trans_a
    # shape constraints
    a_dim = -2 if trans_a else -1
    b_dim = -1 if trans_b else -2
    Ka, Kb = a.shape[a_dim], b.shape[b_dim]
    if Ka != Kb:
        raise ValueError(f"Inner dimension mismatch (A: {Ka} vs B: {Kb})")
    if Ka % 16 != 0:
        raise ValueError('Reduction size for SDD must be a multiple of 16')
    # allocate output
    n_blocks = sum([width * pack * pack for width, pack in zip(widths, packs)])
    c = torch.zeros((a.shape[0], n_blocks, block, block), dtype=a.dtype, device=a.device)
    # each iteration of the loop below
    # computes the value for one group of super-blocks
    # (e.g., all 4x4 super-blocks)
    for lut, width, pack in zip(luts, widths, packs):
        # maximum grid size in Triton/CUDA is 64k but we may have more
        # super-blocks than that.
        max_grid = 65535
        for off_grid in range(0, width, max_grid):
            grid = [1, min(max_grid, width - off_grid), c.shape[0]]
            # fmt: off
            _sdd_kernel[grid](
                a, b, c,
                a.stride(0), a.stride(1), a.stride(3 if trans_a else 2), a.stride(2 if trans_a else 3),
                b.stride(0), b.stride(1), b.stride(3 if trans_b else 2), b.stride(2 if trans_b else 3),
                c.stride(0), c.stride(2), c.stride(3),
                Ka, off_grid, lut,
                TILE_M = block*pack, TILE_N = block*pack, TILE_K = 32, BLOCK = block,
                num_warps=4,
            )
    return c

def sdd_lut(layout, block, device):
    start_width = 128 // block
    layout = layout.type(torch.int32)
    superblocks = libtriton.superblock(layout.data_ptr(), layout.shape[0], layout.shape[1], layout.shape[2], start_width)
    luts, widths, packs = [], [], []
    for size, nnz in superblocks:
        nnz = nnz.reshape(-1, 4)
        width = nnz.shape[0] // (size * size)
        luts.append(torch.from_numpy(nnz).type(torch.int32).to(device))
        widths.append(width)
        packs.append(size)
    return luts, None, widths, packs

# -----------------------------
# Dense = Sparse x Dense (DSD)
# -----------------------------
@triton.jit
def _dsd_kernel(
    A, B, C, 
    stride_az, stride_am, stride_ak, 
    stride_zb, stride_hb, stride_bk, stride_bn, 
    stride_zc, stride_hc, stride_cm, stride_cn, 
    DS0, DS1, lut, **meta
):
    TILE_M = meta['TILE_M']
    TILE_N = meta['TILE_N']
    TILE_K = meta['TILE_K']
    BLOCK = meta['BLOCK']
    #------------#
    #- Prologue -#
    #------------#
    pid0   = tl.program_id(0)
    pid1   = tl.program_id(1)
    pidz   = tl.program_id(2)
    header = lut + pid0 * 6
    offset = tl.load(header + 0)
    AS1    = tl.load(header + 1)
    column = tl.load(header + 2)
    off_h  = tl.load(header + 3)
    pinc   = lut + offset
    # initialize pointers to A (sparse)
    off_a   = tl.load(pinc + 1)
    off_a   = tl.multiple_of(off_a, 8)  # compiler hint
    off_a   = off_a * BLOCK * BLOCK
    offs_am = tl.arange(0, TILE_M)
    offs_ak = tl.arange(0, TILE_K)
    pa = A + off_a \
            + pidz * stride_az \
            + offs_am[:, None] * stride_am \
            + offs_ak[None, :] * stride_ak
    # initialize pointers to B (dense)
    offs_bn  = pid1*TILE_N + tl.arange(0, TILE_N)
    start_bk = tl.load(pinc)
    start_bk = tl.multiple_of(start_bk, 8)  # compiler hint
    offs_bk  = start_bk + tl.arange(0, TILE_K)
    pb = B + pidz * stride_zb \
            + off_h * stride_hb \
            + offs_bn[None, :] * stride_bn \
            + offs_bk[:, None] * stride_bk
    ## ---------------- ##
    ##    Inner Loop    ##
    ## ---------------- ##
    acc = tl.zeros((TILE_M, TILE_N), dtype=tl.float32)
    for k in range(AS1, 0, -TILE_K):
        a = tl.load(pa, mask=True)
        b = tl.load(pb, mask=offs_bn[None, :] < DS0)
        acc += tl.dot(a, b)
        pinc += 2
        inc_a = tl.load(pinc + 1)
        inc_a = tl.multiple_of(inc_a, 8)
        inc_b = tl.load(pinc)
        inc_b = tl.multiple_of(inc_b, 8)
        inc_b = inc_b * stride_bk
        pa += inc_a
        pb += inc_b
    c = acc.to(C.dtype.element_ty)
    # initialize pointers to C
    offs_cm = column*TILE_M + tl.arange(0, TILE_M)
    offs_cn = pid1*TILE_N + tl.arange(0, TILE_N)
    pc = C + off_h * stride_hc \
            + pidz * stride_zc \
            + offs_cm[:, None] * stride_cm \
            + offs_cn[None, :] * stride_cn
    tl.store(pc, c, mask = offs_cn[None, :] < DS0)

def dsd_matmul(a, b, trans_a, trans_b, trans_c, spdims, block, lut, num_locks, width, packs):
    # shapes / dtypes
    AS1 = block * spdims[2 if trans_a else 1]
    BS0 = b.size(0)
    BS1 = b.size(1)
    BS3 = b.size(2 if trans_b else 3)
    dtype = a.dtype
    # allocate output
    CS0 = BS0
    CS1 = BS1
    CS2 = BS3 if trans_c else AS1
    CS3 = AS1 if trans_c else BS3
    c = torch.empty((CS0, CS1, CS2, CS3), dtype=dtype, device=a.device)
    # compute output
    # fmt: off
    grid = lambda meta: [width, triton.cdiv(BS3, meta['TILE_N']), BS0]
    _dsd_kernel[grid](
        a, b, c,
        a.stride(0), a.stride(3 if trans_a else 2), a.stride(2 if trans_a else 3),
        b.stride(0), b.stride(1), b.stride(3 if trans_b else 2), b.stride(2 if trans_b else 3),
        c.stride(0), c.stride(1), c.stride(3 if trans_c else 2), c.stride(2 if trans_c else 3),
        BS3, AS1, lut,
        TILE_M = block, TILE_N = 128, TILE_K = 16, BLOCK = block,
        num_warps=4,
    )
    return c

# -----------------------------
# Dense = Dense x Sparse (DDS)
# -----------------------------
@triton.jit
def _dds_kernel(
    A, B, C, 
    stride_za, stride_ha, stride_ma, stride_ka, 
    stride_zb, stride_hb, stride_bk, stride_bn, 
    stride_zc, stride_hc, stride_mc, stride_nc, 
    DS0, DS1, lut, **meta
):
    TILE_M = meta['TILE_M']
    TILE_N = meta['TILE_N']
    TILE_K = meta['TILE_K']
    BLOCK  = meta['BLOCK']
    #------------#
    #- Prologue -#
    #------------#
    pid0 = tl.program_id(0)
    pid1 = tl.program_id(1)
    pidz = tl.program_id(2)
    header = lut + pid0 * 6
    offset = tl.load(header + 0)
    AS1 = tl.load(header + 1)
    column = tl.load(header + 2)
    off_h = tl.load(header + 3)
    pinc = lut + offset
    # initialize pointers to A (dense)
    offs_am = pid1*TILE_M + tl.arange(0, TILE_M)
    start_ak = tl.load(pinc)
    start_ak = tl.multiple_of(start_ak, 8)
    offs_ak = start_ak + tl.arange(0, TILE_K)
    ptrs_a = A + pidz * stride_za \
            + off_h * stride_ha \
            + offs_am[:, None] * stride_ma \
            + offs_ak[None, :] * stride_ka
    # initialize pointers to B (sparse)
    off_b = tl.load(pinc + 1)
    off_b = tl.multiple_of(off_b, 8) 
    off_b = off_b * BLOCK * BLOCK
    offs_bn = tl.arange(0, TILE_N)
    offs_bk = tl.arange(0, TILE_K)
    ptrs_b = B + off_b \
            + pidz * stride_zb \
            + offs_bn[None, :] * stride_bn \
            + offs_bk[:, None] * stride_bk
    ## ---------------- ##
    ##    Inner Loop    ##
    ## ---------------- ##
    acc = tl.zeros((TILE_M, TILE_N), dtype=tl.float32)
    for k in range(AS1, 0, -TILE_K):
        a = tl.load(ptrs_a, mask = offs_am[:, None] < DS0)
        b = tl.load(ptrs_b, mask = True)
        acc += tl.dot(a, b)
        pinc += 2
        inc_a = tl.load(pinc)
        inc_b = tl.load(pinc + 1)
        inc_a = tl.multiple_of(inc_a, 8)
        inc_b = tl.multiple_of(inc_b, 8)
        inc_a = inc_a * stride_ka
        ptrs_a += inc_a
        ptrs_b += inc_b
    ## ---------------- ##
    ##    Epilogue      ##
    ## ---------------- ##
    c = acc.to(C.dtype.element_ty)
    # initialize pointers to C (dense)
    offs_cm = pid1 * TILE_M + tl.arange(0, TILE_M)
    offs_cn = column * TILE_N + tl.arange(0, TILE_N)
    ptrs_c = C + off_h * stride_hc \
            + pidz * stride_zc \
            + offs_cm[:, None] * stride_mc \
            + offs_cn[None, :] * stride_nc
    # write back
    tl.store(ptrs_c, c, mask = offs_cm[:, None] < DS0)

def dds_matmul(a, b, trans_a, trans_b, trans_c, spdims, block, lut, num_locks, width, packs):
    # shapes / dtypes
    AS0 = a.size(0)
    AS1 = a.size(1)
    AS2 = a.size(3 if trans_a else 2)
    BS2 = block * spdims[1 if trans_b else 2]
    dtype = a.dtype
    # output
    CS0 = AS0
    CS1 = AS1
    CS2 = BS2 if trans_c else AS2
    CS3 = AS2 if trans_c else BS2
    c = torch.empty((CS0, CS1, CS2, CS3), dtype=dtype, device=a.device)
    grid = lambda meta: [width, triton.cdiv(AS2, meta['TILE_M']), AS0]
    # fmt: off
    _dds_kernel[grid](
        a, b, c,
        a.stride(0), a.stride(1), a.stride(3 if trans_a else 2), a.stride(2 if trans_a else 3),
        b.stride(0), b.stride(1), b.stride(3 if trans_b else 2), b.stride(2 if trans_b else 3),
        c.stride(0), c.stride(1), c.stride(3 if trans_c else 2), c.stride(2 if trans_c else 3),
        AS2, BS2, lut,
        TILE_M = 128, TILE_N = block, TILE_K = 16, BLOCK = block,
        num_warps=4
    )
    return c

##############
#  MAIN API  #
##############
class _matmul(torch.autograd.Function):

    locks = dict()

    # Given an array sizes representing reduction size for each
    # column of a block-mode matrix multiplication,
    # performs load-balancing to achieve more smaller reductions
    # between `seg_size` elements
    @staticmethod
    def load_balance(sizes):
        # segment size
        # heuristics taken from OpenAI blocksparse code
        # https://github.com/openai/blocksparse/blob/master/blocksparse/matmul.py#L95
        max_size = sizes.max()
        min_size = sizes[sizes != 0].min()
        #if max_size > min_size * 2.0:
        #  seg_max = max(triton.cdiv(max_size, 4), min_size*2)
        #else:
        #  seg_max = max_size
        seg_max = max_size
        seg_min = max(triton.cdiv(seg_max, 4), 4)
        # split reduction into segments
        div = sizes // seg_max
        rem = sizes % seg_max
        packs = div + (sizes < seg_min).long() + (rem >= seg_min).long()
        width = packs.sum()
        segments = torch.empty(width, dtype=sizes.dtype)
        column = torch.empty_like(segments)
        lockid = torch.zeros_like(segments)
        maxid = torch.zeros_like(segments)
        nlocks = 0
        current = 0
        col_idx = 0
        for i in range(len(sizes)):
            d, r = div[i], rem[i]
            isempty = sizes[i] < seg_min
            last = current + d + (r >= seg_min) + isempty
            # column id
            column[current:last] = col_idx
            # lock id
            if d > 1 or (d == 1 and r >= seg_min):
                nlocks += 1
                lockid[current:last] = nlocks
                maxid[current:last] = last - current
            # segment size
            segments[current:current + d] = seg_max
            if r < seg_min and not isempty:
                segments[current + d - 1] += r
            if r >= seg_min or isempty:
                segments[current + d] = r
            current = last
            col_idx += 1
        offsets = torch.zeros_like(segments)
        offsets[1:] = torch.cumsum(segments[:-1], dim=0)
        return segments, column, lockid, maxid, offsets

    @staticmethod
    def get_locks(size, dev):
        if dev not in _matmul.locks or \
            size > _matmul.locks[dev].size(0):
            _matmul.locks[dev] = torch.zeros(size, dtype=torch.int32, device=dev)
        return _matmul.locks[dev]


    ##########################
    # DENSE = DENSE x SPARSE #
    # DENSE = SPARSE x DENSE #
    ##########################

    # Given a binary layout of 0s and 1s,
    # Construct look-up table for efficient execution on GPUs
    @staticmethod
    def make_dxx_lut(layout, block, step, trans, device, transform=lambda idx: idx):
        # load-balancing
        _empty = torch.tensor([], dtype=torch.int64, device=layout.device)
        segments = _empty.clone()
        column = _empty.clone()
        depth = _empty.clone()
        lockid = _empty.clone()
        maxid = _empty.clone()
        offsets = _empty.clone()
        current_offset = 0
        current_maxid = 0
        for z in range(layout.size(0)):
            if trans:
                sizes = torch.sum(layout[z, :, :], 1)
            else:
                sizes = torch.sum(layout[z, :, :], 0)
            z_segments, z_column, z_lockid, z_maxid, z_offsets = _matmul.load_balance(sizes)
            z_depth = z * torch.ones_like(z_segments)
            z_lockid[z_lockid > 0] += current_maxid
            current_maxid = z_lockid.max()
            # concatenate depth
            segments = torch.cat((segments, z_segments))
            column = torch.cat((column, z_column))
            depth = torch.cat((depth, z_depth))
            maxid = torch.cat((maxid, z_maxid))
            offsets = torch.cat((offsets, current_offset + z_offsets))
            lockid = torch.cat((lockid, z_lockid))
            current_offset += layout[z, :, :].sum()
        segments *= step
        # pointer increments
        if trans:
            nnz = layout.nonzero(as_tuple=False)
        else:
            nnz = layout.transpose(1, 2).nonzero(as_tuple=False)
        num_blocks = nnz.size(0)
        offsets = torch.min(offsets, (num_blocks - 1) * torch.ones_like(offsets))
        idx = transform(nnz[:, 2] * block)
        xincs = idx.clone()
        xincs[1:] -= idx[:-1]
        # divide block into multiple steps
        div = block // step
        xincs = xincs.view(-1, 1).repeat(1, div)
        xincs[:, 1:] = step
        xincs[:, 0] -= (div - 1) * step
        # first increment for each reduction is actually the offset
        xincs[offsets[segments > 0], 0] = idx[offsets[segments > 0]]
        xincs = xincs.view(-1)
        # block-mode input increments
        if trans:
            widx = torch.arange(num_blocks)
        else:
            widx = _empty.clone()
            current_offset = 0
            for z in range(layout.size(0)):
                layoutw = layout[z, :, :].clone()
                msum = layoutw.sum()
                layoutw[layoutw > 0] = 1 + torch.arange(msum)
                widx = torch.cat((widx, current_offset + layoutw.T[layoutw.T > 0] - 1))
                current_offset += msum
        widx = widx
        wincs = widx * block * block
        wincs[1:] -= widx[:-1] * block * block
        wincs = wincs.view(-1, 1).repeat(1, div)
        if trans:
            wincs[:, 1:] = step
            wincs[:, 0] -= (div - 1) * step
        else:
            wincs[:, 1:] = step * block
            wincs[:, 0] -= (div - 1) * step * block
        wincs[offsets[segments > 0], 0] = widx[offsets[segments > 0]]
        wincs = wincs.view(-1)
        # adjust offset and segment size
        offsets *= 2 * div
        segments *= div
        # create header
        width = column.size(0)
        offsets += 6 * width
        header = torch.stack((offsets, segments, column, depth, lockid, maxid), dim=1).view(-1).contiguous()
        incs = torch.stack((xincs, wincs), dim=1).view(-1).contiguous()
        incs = torch.cat((incs, torch.zeros(2, device=incs.device, dtype=incs.dtype)))
        # create lut
        lut = torch.cat((header, incs))
        lut = lut.type(torch.int32).to(device)
        # create locks
        num_locks = max(1, lockid.max())
        return lut, num_locks, width, None

    fn = {'sdd': sdd_matmul, 'dsd': dsd_matmul, 'dds': dds_matmul}

    @staticmethod
    def forward(
        ctx, a, b, trans_a, trans_b, trans_c, mode, spdims, block, c_lut, c_num_locks, c_width, c_packs, da_lut, da_num_locks,
        da_width, da_packs, db_lut, db_num_locks, db_width, db_packs
    ):
        c = _matmul.fn[mode](a, b, trans_a, trans_b, trans_c, spdims, block, c_lut, c_num_locks, c_width, c_packs)
        # save for backward
        ctx.save_for_backward(a, b)
        ctx.da_num_locks = da_num_locks
        ctx.da_lut = da_lut
        ctx.da_width = da_width
        ctx.da_packs = da_packs
        ctx.db_lut = db_lut
        ctx.db_num_locks = db_num_locks
        ctx.db_width = db_width
        ctx.db_packs = db_packs
        ctx.mode = mode
        ctx.spdims = spdims
        ctx.block = block
        ctx.trans_a = trans_a
        ctx.trans_b = trans_b
        return c

    @staticmethod
    def backward(ctx, dc):
        # saved for backward
        a, b = ctx.saved_tensors
        da, db = None, None
        mode = ctx.mode

        # gradients w.r.t. a
        if ctx.needs_input_grad[0]:
            mode_da = mode[1] + mode[0] + mode[2]
            da = _matmul.fn[mode_da](
                dc, b, False, not ctx.trans_b, ctx.trans_a, ctx.spdims, ctx.block, ctx.da_lut, ctx.da_num_locks, ctx.da_width,
                ctx.da_packs
            )
        # gradients w.r.t. b
        if ctx.needs_input_grad[1]:
            mode_db = mode[2] + mode[1] + mode[0]
            db = _matmul.fn[mode_db](
                a, dc, not ctx.trans_a, False, ctx.trans_b, ctx.spdims, ctx.block, ctx.db_lut, ctx.db_num_locks, ctx.db_width,
                ctx.db_packs
            )
        return da, db, None, None, None,\
               None, None, None, None,\
               None, None, None, None, None, None,\
               None, None, None, None, None, None,\
               None, None, None, None, None, None


class matmul:
    def make_lut(self, dtype, device):
        key = (dtype, device)
        if key in self.lut_cache:
            return self.lut_cache[key]
        # C look-up table
        layout, block = self.layout, self.block
        step = 16
        if self.mode == 'sdd':
            c_lut, c_num_locks, c_width, c_packs = sdd_lut(layout, block, device)
        elif self.mode == 'dsd':
            c_lut, c_num_locks, c_width, c_packs = _matmul.make_dxx_lut(layout, block, step, not self.trans_a, device)
        elif self.mode == 'dds':
            c_lut, c_num_locks, c_width, c_packs = _matmul.make_dxx_lut(layout, block, step, self.trans_b, device)
        # DA look-up table
        if self.mode == 'sdd':
            da_lut, da_num_locks, da_width, da_packs = _matmul.make_dxx_lut(layout, block, step, True, device)
        elif self.mode == 'dsd':
            da_lut, da_num_locks, da_width, da_packs = sdd_lut(layout, block, device)
        elif self.mode == 'dds':
            da_lut, da_num_locks, da_width, da_packs = _matmul.make_dxx_lut(layout, block, step, not self.trans_b, device)
        # DB look-up table
        if self.mode == 'sdd':
            db_lut, db_num_locks, db_width, db_packs = _matmul.make_dxx_lut(layout, block, step, False, device)
        elif self.mode == 'dsd':
            db_lut, db_num_locks, db_width, db_packs = _matmul.make_dxx_lut(layout, block, step, self.trans_a, device)
        elif self.mode == 'dds':
            db_lut, db_num_locks, db_width, db_packs = sdd_lut(layout, block, device)
        self.lut_cache[key] = (c_lut, c_num_locks, c_width, c_packs,
                               da_lut, da_num_locks, da_width, da_packs,
                               db_lut, db_num_locks, db_width, db_packs)
        return self.lut_cache[key]

    def __init__(self, layout, block, mode, trans_a=False, trans_b=False):
        if mode not in ['sdd', 'dsd', 'dds']:
            raise NotImplementedError('Supported modes are: sdd, dsd, dds')
        # look-up table cache
        self.lut_cache = dict()
        # attributes
        self.block = block
        self.mode = mode
        self.trans_a = trans_a
        self.trans_b = trans_b

        layout_dim = layout.ndim
        assert layout_dim in (2, 3), "Layout should be a 2 or 3 dimensional tensor of 0s and 1s"

        if not mode == 'sdd':
            # Dims to be reduced on the 'inside' of the matmul, either -1 or -2
            trans_dense, trans_sparse, sparse_inner = (trans_b, trans_a, -1) if mode == 'dsd' else (trans_a, trans_b, -2)
            self.dense_inner_dim = -((sparse_inner % 2) + 1) if not trans_dense else sparse_inner
            sparse_inner = sparse_inner if not trans_sparse else -((sparse_inner % 2) + 1)

            # Inner dim of the dense input should be equal to the inner dim of the sparse input
            self.dense_inner_size = layout.shape[sparse_inner] * block
            # Expected shape for sparse inputs
            self.sparse_shape = (layout.sum().item(), block, block)

        # Support using the same layout across attention heads etc.
        if layout_dim == 2:
            layout = layout.unsqueeze(0)

        layout = layout.long()  # Above code assumes the layout tensor is an integral type
        self.layout = layout
        self.spdims = layout.shape

    def __call__(self, a, b):
        c_lut, c_num_locks, c_width, c_packs,\
        da_lut, da_num_locks, da_width, da_packs,\
        db_lut, db_num_locks, db_width, db_packs = self.make_lut(a.dtype, a.device)

        # If we don't check for invalid shapes, devices, & dtypes here, they will lead to undefined behavior
        # and potential illegal memory accesses
        original_dims = max(a.ndim, b.ndim)
        a, b = self._validate_inputs(a, b)

        # execute
        c = _matmul.apply(
            a, b, self.trans_a, self.trans_b, False, self.mode, self.spdims, self.block, c_lut, c_num_locks, c_width,
            c_packs, da_lut, da_num_locks, da_width, da_packs, db_lut, db_num_locks, db_width, db_packs
        )
        # This removes any leading singleton dimensions we may have added to the tensor that weren't in the input
        dims_to_trim = c.ndim - original_dims
        for _ in range(dims_to_trim):
            c = c.squeeze(0)

        return c

    def _validate_inputs(self, a, b):
        if a.device != b.device:
            raise ValueError(f"Inputs must be on the same device; got {a.device} for tensor A "
                             f"and {b.device} for tensor B")
        if not a.is_cuda:
            raise ValueError("Only GPU devices are supported for now")

        # When autocast is enabled, torch.matmul autocasts to float16, so we do the same here
        if torch.is_autocast_enabled():
            a, b = a.half(), b.half()
        elif a.dtype != b.dtype:
            raise ValueError(f"Inputs must be the same dtype; got {a.dtype} for A and {b.dtype} for B")

        mode, trans_a, trans_b = self.mode, self.trans_a, self.trans_b
        if mode != 'sdd':
            # One input is sparse
            dense, dense_name, sparse, sparse_name = (a, 'A', b, 'B') if mode == 'dds' else (b, 'B', a, 'A')
            dense_inner = dense.shape[self.dense_inner_dim]
            if dense_inner != self.dense_inner_size:
                raise ValueError(f"Expected tensor {dense_name} to have size {self.dense_inner_size} at dim "
                                 f"{self.dense_inner_dim % dense.ndim}, got {dense_inner}.")

            if sparse.shape[-len(self.sparse_shape):] != self.sparse_shape:
                raise ValueError(f"Expected tensor with trailing dimensions of shape {self.sparse_shape} for argument "
                                 f"{sparse_name}, got {sparse.shape}")

        def add_extra_dims(x):
            # Add extra leading singleton dimensions if needed
            dims_needed = 4 - x.ndim
            if dims_needed > 0:
                singletons = [1] * dims_needed
                x = x.view(*singletons, *x.shape)
            elif dims_needed < 0:
                raise ValueError("Tensors with more than 4 dimensions are not currently supported")

            return x

        # Pad shapes with leading singleton dimensions
        a = add_extra_dims(a)
        b = add_extra_dims(b)

        return a, b

