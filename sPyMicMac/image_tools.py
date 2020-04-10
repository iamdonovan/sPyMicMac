"""
sPyMicMac.image_tools is a collection of tools for working with KH-9 Hexagon imagery.
"""
import os
from glob import glob
import cv2
from itertools import chain
import gdal
from skimage.morphology import disk
from skimage.filters import rank
from skimage import exposure
from skimage.measure import ransac
from skimage.transform import match_histograms, warp, AffineTransform, EuclideanTransform
from scipy.interpolate import RectBivariateSpline as RBS
from scipy import ndimage
import numpy as np
from shapely.ops import cascaded_union
import geopandas as gpd
import pyvips
from llc import jit_filter_function
from pybob.image_tools import match_hist, reshape_geoimg, create_mask_from_shapefile


######################################################################################################################
# image filtering tools
######################################################################################################################
@jit_filter_function
def nanstd(a):
    return np.nanstd(a)


def cross_template(shape, width=3):
    if isinstance(shape, int):
        rows = shape
        cols = shape
    else:
        rows, cols = shape
    half_r = int((rows-1)/2)
    half_c = int((cols-1)/2)
    half_w = int((width-1)/2)

    cross = np.zeros((rows, cols))
    cross[half_r-half_w-1:half_r+half_w+2:width+1, :] = 2
    cross[:, half_c-half_w-1:half_c+half_w+2:width+1] = 2

    cross[half_r-half_w:half_r+half_w+1, :] = 1
    cross[:, half_c-half_w:half_c+half_w+1] = 1
    return cross


def cross_filter(img, cross):
    cross_edge = cross == 2 
    cross_cent = cross == 1 
    edge_std = ndimage.filters.generic_filter(highpass_filter(img), nanstd, footprint=cross_edge) 
    cent_std = ndimage.filters.generic_filter(highpass_filter(img), nanstd, footprint=cross_cent) 
    return np.where(np.logical_and(edge_std != 0, cent_std != 0), cent_std / edge_std, 2) 


def make_template(img, pt, half_size):
    nrows, ncols = img.shape
    row, col = np.round(pt).astype(int)
    left_col = max(col - half_size, 0)
    right_col = min(col + half_size, ncols)
    top_row = max(row - half_size, 0)
    bot_row = min(row + half_size, nrows)
    row_inds = [row - top_row, bot_row - row]
    col_inds = [col - left_col, right_col - col]
    template = img[top_row:bot_row+1, left_col:right_col+1].copy()
    return template, row_inds, col_inds


def find_match(img, template):
    img_eq = rank.equalize(img, selem=disk(100))
    # res = cross_filter(img_eq, template)
    res = cv2.matchTemplate(img_eq, template, cv2.TM_CCORR_NORMED)
    i_off = (img.shape[0] - res.shape[0])/2
    j_off = (img.shape[1] - res.shape[1])/2
    minval, _, minloc, _ = cv2.minMaxLoc(res)
    # maxj, maxi = maxloc
    minj, mini = minloc
    sp_delx, sp_dely = get_subpixel(res)
    # sp_delx, sp_dely = 0, 0
    return res, mini + i_off + sp_dely, minj + j_off + sp_delx


def get_subpixel(res, how='min'):
    assert how in ['min', 'max'], "have to choose min or max"

    mgx, mgy = np.meshgrid(np.arange(-1, 1.01, 0.1), np.arange(-1, 1.01, 0.1), indexing='xy')  # sub-pixel mesh

    if how == 'min':
        peakval, _, peakloc, _ = cv2.minMaxLoc(res)
        mml_ind = 2
    else:
        _, peakval, _, peakloc = cv2.minMaxLoc(res)
        mml_ind = 3

    rbs_halfsize = 3  # size of peak area used for spline for subpixel peak loc
    rbs_order = 4    # polynomial order for subpixel rbs interpolation of peak location

    if((np.array([n-rbs_halfsize for n in peakloc]) >= np.array([0, 0])).all()
                & (np.array([(n+rbs_halfsize) for n in peakloc]) < np.array(list(res.shape))).all()):
        rbs_p = RBS(range(-rbs_halfsize, rbs_halfsize+1), range(-rbs_halfsize, rbs_halfsize+1),
                    res[(peakloc[1]-rbs_halfsize):(peakloc[1]+rbs_halfsize+1),
                        (peakloc[0]-rbs_halfsize):(peakloc[0]+rbs_halfsize+1)],
                    kx=rbs_order, ky=rbs_order)

        b = rbs_p.ev(mgx.flatten(), mgy.flatten())
        mml = cv2.minMaxLoc(b.reshape(21, 21))
        # mgx,mgy: meshgrid x,y of common area
        # sp_delx,sp_dely: subpixel delx,dely
        sp_delx = mgx[mml[mml_ind][0], mml[mml_ind][1]]
        sp_dely = mgy[mml[mml_ind][0], mml[mml_ind][1]]
    else:
        sp_delx = 0.0
        sp_dely = 0.0
    return sp_delx, sp_dely


def highpass_filter(img):
    v = img.copy()
    v[np.isnan(img)] = 0
    vv = ndimage.gaussian_filter(v, 3)

    w = 0 * img.copy() + 1
    w[np.isnan(img)] = 0
    ww = ndimage.gaussian_filter(w, 3)

    tmplow = vv / ww
    tmphi = img - tmplow
    return tmphi


def splitter(img, nblocks, overlap=0):
    split1 = np.array_split(img, nblocks[0], axis=0)
    split2 = [np.array_split(im, nblocks[1], axis=1) for im in split1]
    olist = [np.copy(a) for a in list(chain.from_iterable(split2))]
    return olist


def get_subimg_offsets(split, shape):
    ims_x = np.array([s.shape[1] for s in split])
    ims_y = np.array([s.shape[0] for s in split])

    rel_x = np.cumsum(ims_x.reshape(shape), axis=1)
    rel_y = np.cumsum(ims_y.reshape(shape), axis=0)

    rel_x = np.concatenate((np.zeros((shape[0], 1)), rel_x[:, :-1]), axis=1)
    rel_y = np.concatenate((np.zeros((1, shape[1])), rel_y[:-1, :]), axis=0)

    return rel_x.astype(int), rel_y.astype(int)


######################################################################################################################
# GCP matching tools
######################################################################################################################
def get_dense_keypoints(img, mask, npix=200, return_des=False):
    orb = cv2.ORB_create()
    keypts = []
    if return_des:
        descriptors = []

    x_tiles = np.floor(img.shape[1] / npix).astype(int)
    y_tiles = np.floor(img.shape[0] / npix).astype(int)

    split_img = splitter(img, (y_tiles, x_tiles))
    split_msk = splitter(mask, (y_tiles, x_tiles))

    rel_x, rel_y = get_subimg_offsets(split_img, (y_tiles, x_tiles))

    for i, img_ in enumerate(split_img):
        iy, ix = np.unravel_index(i, (y_tiles, x_tiles))

        ox = rel_x[iy, ix]
        oy = rel_y[iy, ix]

        kp, des = orb.detectAndCompute(img_, mask=split_msk[i])
        if return_des:
            if des is not None:
                for ds in des:
                    descriptors.append(ds)

        for p in kp:
            p.pt = p.pt[0] + ox, p.pt[1] + oy
            keypts.append(p)

    if return_des:
        return keypts, descriptors
    else:
        return keypts


def get_footprint_mask(shpfile, geoimg, filelist, fprint_out=False):
    imlist = [im.split('OIS-Reech_')[-1].split('.tif')[0] for im in filelist]
    footprints_shp = gpd.read_file(shpfile)
    fp = footprints_shp[footprints_shp.ID.isin(imlist)]
    fp.sort_values('ID', inplace=True)
    if fp.shape[0] > 3:
        fprint = cascaded_union(fp.to_crs(epsg=geoimg.epsg).geometry.values[1:-1]).minimum_rotated_rectangle
    elif fp.shape[0] == 3:
        fprint = fp.to_crs(epsg=geoimg.epsg).geometry.values[1].minimum_rotated_rectangle
    else:
        fprint = fp.to_crs(epsg=geoimg.epsg).geometry.values[0].intersection(fp.to_crs(epsg=geoimg.epsg).geometry.values[1]).minimum_rotated_rectangle

    tmp_gdf = gpd.GeoDataFrame(columns=['geometry'])
    tmp_gdf.loc[0, 'geometry'] = fprint
    tmp_gdf.crs = {'init': 'epsg:{}'.format(geoimg.epsg)}
    tmp_gdf.to_file('tmp_fprint.shp')

    maskout = create_mask_from_shapefile(geoimg, 'tmp_fprint.shp')

    for f in glob('tmp_fprint.*'):
        os.remove(f)
    if fprint_out:
        return maskout, fprint
    else:
        return maskout


def get_rough_geotransform(img1, img2, pRes=800, landmask=None):
    img2_lowres = img2.resample(pRes, method=gdal.GRA_NearestNeighbour)

    img2_eq = (255 * exposure.equalize_adapthist(img2_lowres.img.astype(np.uint16), clip_limit=0.03)).astype(np.uint8)
    img1_mask = 255 * np.ones(img1.shape, dtype=np.uint8)
    img1_mask[img1 == 0] = 0

    img2_mask = 255 * np.ones(img2_eq.shape, dtype=np.uint8)
    img2_mask[np.isnan(img2_lowres.img)] = 0

    if landmask is not None:
        lm = create_mask_from_shapefile(img2_lowres, landmask)
        img2_mask[~lm] = 0

    kp, des, matches = get_matches(img1, img2_eq, mask1=img1_mask, mask2=img2_mask)
    src_pts = np.array([kp[0][m.queryIdx].pt for m in matches])
    dst_pts = np.array([kp[1][m.trainIdx].pt for m in matches])

    Minit, inliers = ransac((dst_pts, src_pts), EuclideanTransform,
                            min_samples=5, residual_threshold=2, max_trials=1000)
    print('{} matches used for initial transformation'.format(np.count_nonzero(inliers)))

    img1_tfm = warp(img1, Minit, output_shape=img2_eq.shape, preserve_range=True)

    return img1_tfm, Minit, (dst_pts, src_pts, inliers)


def get_initial_transformation(img1, img2, pRes=800, landmask=None, footmask=None, imlist=None):
    im2_lowres = reshape_geoimg(img2, pRes, pRes)

    im2_eq = match_hist(im2_lowres.img, np.array(img1))
    im1_mask = 255 * np.ones(img1.shape, dtype=np.uint8)
    im1_mask[img1 == 0] = 0  # nodata from ortho

    im2_mask = 255 * np.ones(im2_eq.shape, dtype=np.uint8)
    im2_mask[im2_eq == 0] = 0

    if imlist is None:
        imlist = glob('OIS*.tif')

    if landmask is not None:
        mask_ = create_mask_from_shapefile(im2_lowres, landmask)
        im2_mask[~mask_] = 0
    if footmask is not None:
        mask_ = get_footprint_mask(footmask, im2_lowres, imlist)
        im2_mask[~mask_] = 0

    kp, des, matches = get_matches(img1, im2_eq, mask1=im1_mask, mask2=im2_mask)
    src_pts = np.array([kp[0][m.queryIdx].pt for m in matches])
    dst_pts = np.array([kp[1][m.trainIdx].pt for m in matches])
    # aff_matrix, good_mask = cv2.estimateAffine2D(src_pts, dst_pts, ransacReprojThreshold=25)
    Mout, inliers = ransac((dst_pts, src_pts), EuclideanTransform,
                           min_samples=5, residual_threshold=2, max_trials=1000)
    # check that the transformation was successful by correlating the two images.
    # im1_tfm = cv2.warpAffine(img1, aff_matrix, (im2_lowres.img.shape[1], im2_lowres.img.shape[0]))
    im1_tfm = warp(img1, Mout, output_shape=im2_lowres.img.shape, preserve_range=True)
    im1_pad = np.zeros(np.array(im1_tfm.shape)+2, dtype=np.uint8)
    im1_pad[1:-1, 1:-1] = im1_tfm
    res = cv2.matchTemplate(np.ma.masked_values(im2_eq, 0),
                            np.ma.masked_values(im1_pad, 0),
                            cv2.TM_CCORR_NORMED)
    print(res[1,1])
    success = res[1, 1] > 0.5

    return Mout, success, im2_eq.shape


def get_matches(img1, img2, mask1=None, mask2=None):
    orb = cv2.ORB_create()

    kp1, des1 = orb.detectAndCompute(img1.astype(np.uint8), mask=mask1)
    kp2, des2 = orb.detectAndCompute(img2.astype(np.uint8), mask=mask2)

    flann_idx = 6
    index_params = dict(algorithm=flann_idx, table_number=6, key_size=12, multi_probe_level=1)
    search_params = dict(checks=100)

    flann = cv2.FlannBasedMatcher(index_params, search_params)
    raw_matches = flann.knnMatch(des1, des2, k=2)
    matches = []
    for m in raw_matches:
        if len(m) == 2 and m[0].distance < m[1].distance * 0.75:
            matches.append(m[0])

    return (kp1, kp2), (des1, des2), matches


def find_gcp_match(img, template, method=cv2.TM_CCORR_NORMED):
    res = cv2.matchTemplate(img, template, method)
    i_off = (img.shape[0] - res.shape[0]) / 2
    j_off = (img.shape[1] - res.shape[1]) / 2
    _, maxval, _, maxloc = cv2.minMaxLoc(res)
    maxj, maxi = maxloc
    sp_delx, sp_dely = get_subpixel(res, how='max')

    return res, maxi + i_off + sp_dely, maxj + j_off + sp_delx


######################################################################################################################
# image writing
######################################################################################################################
def join_halves(img, overlap, indir='.', outdir='.', color_balance=True):
    """
    Join scanned halves of KH-9 image into one, given a common overlap point.
    
    :param img: KH-9 image name (i.e., DZB1215-500454L001001) to join. The function will look for open image halves
        img_a.tif and img_b.tif, assuming 'a' is the left-hand image and 'b' is the right-hand image.
    :param overlap: Image coordinates for a common overlap point, in the form [x1, y1, x2, y2]. Best results tend to be
        overlaps toward the middle of the y range. YMMV.
    :param indir: Directory containing images to join ['.']
    :param outdir: Directory to write joined image to ['.']
    :param color_balance: Attempt to color balance the two image halves before joining [True].

    :type img: str
    :type overlap: array-like
    :type indir: str
    :type outdir: str
    :type color_balance: bool
    """

    left = pyvips.Image.new_from_file(os.path.sep.join([indir, '{}_a.tif'.format(img)]), memory=True)
    right = pyvips.Image.new_from_file(os.path.sep.join([indir, '{}_b.tif'.format(img)]), memory=True)
    outfile = os.path.sep.join([outdir, '{}.tif'.format(img)])

    if len(overlap) < 4:
        x1, y1 = overlap
        if x1 < 0:
            join = left.merge(right, 'horizontal', x1, y1)
        else:
            join = right.merge(left, 'horizontal', x1, y1)

        join.write_to_file(outfile)
    else:
        x1, y1, x2, y2 = overlap

        join = left.mosaic(right, 'horizontal', x1, y1, x2, y2, mblend=0)
        if color_balance:
            balance = join.globalbalance(int_output=True)
            balance.write_to_file(outfile)
        else:
            join.write_to_file(outfile)

    return
