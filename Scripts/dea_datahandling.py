## dea_datahandling.py
'''
Description: This file contains a set of python functions for handling
Digital Earth Australia data.

License: The code in this notebook is licensed under the Apache License,
Version 2.0 (https://www.apache.org/licenses/LICENSE-2.0). Digital Earth
Australia data is licensed under the Creative Commons by Attribution 4.0
license (https://creativecommons.org/licenses/by/4.0/).

Contact: If you need assistance, please post a question on the Open Data
Cube Slack channel (http://slack.opendatacube.org/) or on the GIS Stack
Exchange (https://gis.stackexchange.com/questions/ask?tags=open-data-cube)
using the `open-data-cube` tag (you can view previously asked questions
here: https://gis.stackexchange.com/questions/tagged/open-data-cube).

If you would like to report an issue with this script, you can file one on
Github (https://github.com/GeoscienceAustralia/dea-notebooks/issues/new).

Functions included:
    load_ard
    array_to_geotiff
    mostcommon_utm
    download_unzip
    wofs_fuser
    dilate
    pan_sharpen_brovey
    paths_to_datetimeindex

Last modified: February 2020

'''

# Import required packages
import os
import gdal
import zipfile
import numexpr
import datetime
import requests
import warnings
import odc.algo
import numpy as np
import pandas as pd
import xarray as xr
from collections import Counter
from scipy.ndimage import binary_dilation


def _split_dc_params(**kw):
    """ Partition parameters meant for `dc.load(..)` into query-time and load-time.
    Note that some parameters are used for both.

    Returns
    =======

    (query: dict, load: dict)
    """
    _nothing = object()

    def _impl(measurements=_nothing,
              output_crs=_nothing,
              resolution=_nothing,
              resampling=_nothing,
              skip_broken_datasets=_nothing,
              dask_chunks=_nothing,
              like=_nothing,
              fuse_func=_nothing,
              align=_nothing,
              datasets=_nothing,
              progress_cbk=_nothing,
              **query):
        if like is not _nothing:
            query = dict(like=like, **query)

        load_args = {k: v for k, v in locals().items() if v is not _nothing}
        load_args.pop('query')

        return query, load_args

    return _impl(**kw)


def load_ard(dc,
             products=None,
             min_gooddata=0.0,
             fmask_categories=['valid', 'snow', 'water'],
             mask_pixel_quality=True,
             mask_contiguity='nbart_contiguity',
             ls7_slc_off=True,
             filter_func=None,
             **extras):

    '''
    Loads Landsat Collection 3 or Sentinel 2 Definitive and Near Real
    Time data for multiple sensors (i.e. ls5t, ls7e and ls8c for
    Landsat; s2a and s2b for Sentinel 2), and returns a single masked
    xarray dataset containing only observations that contain greater
    than a given proportion of good quality pixels. This can be used
    to extract clean time series of observations that are not affected
    by cloud, for example as an input to the `animated_timeseries`
    function from `dea_plotting`.

    The proportion of good quality pixels is calculated by summing the
    pixels flagged as good quality in `fmask`. By default non-cloudy or
    shadowed land, snow and water pixels are treated as good quality,
    but this can be customised using the `fmask_gooddata` parameter.

    Last modified: February 2020

    Parameters
    ----------
    dc : datacube Datacube object
        The Datacube to connect to, i.e. `dc = datacube.Datacube()`.
        This allows you to also use development datacubes if required.
    products : list
        A list of product names to load data from. Valid options are
        ['ga_ls5t_ard_3', 'ga_ls7e_ard_3', 'ga_ls8c_ard_3'] for Landsat,
        ['s2a_ard_granule', 's2b_ard_granule'] for Sentinel 2 Definitive,
        and ['s2a_nrt_granule', 's2b_nrt_granule'] for Sentinel 2 Near
        Real Time (on the DEA Sandbox only).
    min_gooddata : float, optional
        An optional float giving the minimum percentage of good quality
        pixels required for a satellite observation to be loaded.
        Defaults to 0.0 which will return all observations regardless of
        pixel quality (set to e.g. 0.99 to return only observations with
        more than 99% good quality pixels).
    fmask_categories : list, optional
        An optional list of fmask category names to treat as good
        quality observations in the above `min_gooddata` calculation.
        The default is `['valid', 'snow', 'water']` which will return
        non-cloudy or shadowed land, snow and water pixels. Choose from:
        'nodata', 'valid', 'cloud', 'shadow', 'snow', and 'water'.
    mask_pixel_quality : bool, optional
        An optional boolean indicating whether to apply the good data
        mask to all observations that were not filtered out for having
        less good quality pixels than `min_gooddata`. E.g. if
        `min_gooddata=0.99`, the filtered observations may still contain
        up to 1% poor quality pixels. The default of False simply
        returns the resulting observations without masking out these
        pixels; True masks them and sets them to NaN using the good data
        mask. This will convert numeric values to floating point values
        which can cause memory issues, set to False to prevent this.
    mask_contiguity : str or bool, optional
        An optional string or boolean indicating whether to mask out
        pixels missing data in any band (i.e. "non-contiguous" values).
        Although most missing data issues are resolved by
        `mask_invalid_data`, this step is important for generating
        clean and concistent composite datasets. The default
        is `mask_contiguity='nbart_contiguity'` which will set any
        pixels with non-contiguous values to NaN based on NBART data.
        If you are loading NBAR data instead, you should specify
        `mask_contiguity='nbar_contiguity'` instead. To ignore non-
        contiguous values completely, set `mask_contiguity=False`.
        Be aware that masking out non-contiguous values will convert
        all numeric values to floating point values when -999 values
        are replaced with NaN, which can cause memory issues.
    mask_dtype : numpy dtype, optional
        An optional parameter that controls the data type/dtype that
        layers are coerced to when when `mask_pixel_quality=True` or
        `mask_contiguity=True`. Defaults to `np.float32`, which uses
        approximately 1/2 the memory of `np.float64`.
    ls7_slc_off : bool, optional
        An optional boolean indicating whether to include data from
        after the Landsat 7 SLC failure (i.e. SLC-off). Defaults to
        True, which keeps all Landsat 7 observations > May 31 2003.
    filter_func : function, optional
        An optional function that can be passed in to restrict the
        datasets that are loaded by the function. A filter function
        should take a `datacube.model.Dataset` object as an input (i.e.
        as returned from `dc.find_datasets`), and return a boolean.
        For example, a filter function could be used to return True on
        only datasets acquired in January:
        `dataset.time.begin.month == 1`
    **extras :
        A set of keyword arguments to `dc.load` that define the
        spatiotemporal query used to extract data. This typically
        includes `measurements`, `x`, `y`, `time`, `resolution`,
        `resampling`, `group_by` and `crs`. Keyword arguments can
        either be listed directly in the `load_ard` call like any
        other parameter (e.g. `measurements=['nbart_red']`), or by
        passing in a query kwarg dictionary (e.g. `**query`). For a
        list of possible options, see the `dc.load` documentation:
        https://datacube-core.readthedocs.io/en/latest/dev/api/generate/datacube.Datacube.load.html

    Returns
    -------
    combined_ds : xarray Dataset
        An xarray dataset containing only satellite observations that
        contains greater than `min_gooddata` proportion of good quality
        pixels.

    '''

    #########
    # Setup #
    #########

    query, load_params = _split_dc_params(**extras)

    # We deal with `dask_chunks` separately
    dask_chunks = load_params.pop('dask_chunks', None)

    # Warn user if they combine lazy load with min_gooddata
    if (min_gooddata > 0.0) and dask_chunks is not None:
        warnings.warn("Setting 'min_gooddata' percentage to > 0.0 "
                      "will cause dask arrays to compute when "
                      "loading pixel-quality data to calculate "
                      "'good pixel' percentage. This can "
                      "slow the return of your dataset.")

    # Verify that products were provided, and that only Sentinel-2 or
    # only Landsat products are being loaded at the same time
    if not products:
        raise ValueError("Please provide a list of product names "
                         "to load data from. Valid options are: \n"
                         "['ga_ls5t_ard_3', 'ga_ls7e_ard_3', 'ga_ls8c_ard_3'] "
                         "for Landsat, ['s2a_ard_granule', "
                         "'s2b_ard_granule'] \nfor Sentinel 2 Definitive, or "
                         "['s2a_nrt_granule', 's2b_nrt_granule'] for "
                         "Sentinel 2 Near Real Time")
    elif all(['ls' in product for product in products]):
        product_type = 'ls'
    elif all(['s2' in product for product in products]):
        product_type = 's2'

    # If `measurements` are specified but do not include fmask or
    # contiguity variables, add these to `measurements`
    fmask_band = 'fmask'
    requested_measurements = load_params.pop('measurements', None)
    measurements = requested_measurements.copy() if requested_measurements else None

    if measurements:
        if fmask_band not in measurements:
            measurements.append(fmask_band)

        if mask_contiguity:
            if isinstance(mask_contiguity, bool):
                mask_contiguity = "nbart_contiguity"  # TODO: nbart vs nbar
            if mask_contiguity not in measurements:
                measurements.append(mask_contiguity)

    # If no `measurements` are specified, Landsat ancillary bands are loaded
    # with a 'oa_' prefix, but Sentinel-2 bands are not. As a work-around,
    # we need to rename the default contiguity and fmask bands if loading
    # Landsat data without specifying `measurements`
    elif product_type == 'ls':
        mask_contiguity = f'oa_{mask_contiguity}' if mask_contiguity else False
        fmask_band = f'oa_{fmask_band}'

    #################
    # Find datasets #
    #################

    # Extract datasets for each product using subset of dcload_kwargs
    dataset_list = []

    # Get list of datasets for each product
    print('Finding datasets')
    for product in products:

        # Obtain list of datasets for product
        print(f'    {product}')
        datasets = dc.find_datasets(product=product, **query)

        # Remove Landsat 7 SLC-off observations if ls7_slc_off=False
        if not ls7_slc_off and product == 'ga_ls7e_ard_3':
            print('    Ignoring SLC-off observations for ls7')
            datasets = [i for i in datasets if i.time.begin <
                        datetime.datetime(2003, 5, 31)]

        # Add any returned datasets to list
        dataset_list.extend(datasets)

    # Raise exception if no datasets are returned
    if len(dataset_list) == 0:
        raise ValueError("No data available for query: ensure that "
                         "the products specified have data for the "
                         "time and location requested")

    # If filter_func is specified, use this function to filter the list
    # of datasets prior to load
    if filter_func:
        print(f'Filtering datasets using filter function')
        dataset_list = [ds for ds in dataset_list if filter_func(ds)]

    # Raise exception if filtering removes all datasets
    if len(dataset_list) == 0:
        raise ValueError("No data available after filtering with "
                         "filter function")

    #############
    # Load data #
    #############

    # Note we always load using dask here so that
    # we can lazy load data before filtering by good data
    ds = dc.load(datasets=dataset_list,
                 measurements=measurements,
                 dask_chunks={} if dask_chunks is None else dask_chunks,
                 **load_params)

    ###############
    # Apply masks #
    ###############

    # Calculate pixel quality mask
    pq_mask = odc.algo.fmask_to_bool(ds[fmask_band],
                                     categories=fmask_categories)

    # Generate good quality data mask
    mask = None
    if mask_pixel_quality:
        print('Applying pixel quality/cloud mask')
        mask = pq_mask

    # Generate contiguity mask
    if mask_contiguity:
        print('Applying contiguity mask')
        cont_mask = ds[mask_contiguity] == 1

        # If mask already has data if mask_pixel_quality == True,
        # multiply with cont_mask to perform a logical 'or' operation
        # (keeping only pixels good in both)
        mask = cont_mask if mask is None else mask * cont_mask

    # Mask data if either of the above masks were generated
    if mask is not None:
        ds = odc.algo.keep_good_only(ds, where=mask)

    ####################
    # Filter good data #
    ####################

    # The good data percentage calculation has to load in all `fmask`
    # data, which can be slow. If the user has chosen no filtering
    # by using the default `min_gooddata = 0`, we can skip this step
    # completely to save processing time
    if min_gooddata > 0.0:

        # Compute good data for each observation as % of total pixels
        print('Counting good quality pixels for each time step')
        data_perc = (pq_mask.sum(axis=[1, 2], dtype='int32') /
                     (pq_mask.shape[1] * pq_mask.shape[2]))

        # Filter by `min_gooddata` to drop low quality observations
        total_obs = len(ds.time)
        ds = ds.sel(time=data_perc >= min_gooddata)
        print(f'Filtering to {len(ds.time)} out of {total_obs} '
              f'time steps with at least {min_gooddata:.1%} '
              f'good quality pixels')

    # Drop bands not originally requested by user
    if requested_measurements:
        ds = ds[requested_measurements]

    ###############
    # Return data #
    ###############

    # Set nodata valuses using odc.algo tools to reduce peak memory
    # use when converting data to a float32 dtype
    ds = odc.algo.to_f32(ds)

    # If user supplied dask_chunks, return data as a dask array without
    # actually loading it in
    if dask_chunks is not None:
        print(f'Returning {len(ds.time)} time steps as a dask array')
        return ds
    else:
        print(f'Loading {len(ds.time)} time steps')
        return ds.compute()


def array_to_geotiff(fname, data, geo_transform, projection,
                     nodata_val=0, dtype=gdal.GDT_Float32):
    """
    Create a single band GeoTIFF file with data from an array.

    Because this works with simple arrays rather than xarray datasets
    from DEA, it requires geotransform info ("(upleft_x, x_size,
    x_rotation, upleft_y, y_rotation, y_size)") and projection data
    (in "WKT" format) for the output raster. These are typically
    obtained from an existing raster using the following GDAL calls:

        import gdal
        gdal_dataset = gdal.Open(raster_path)
        geotrans = gdal_dataset.GetGeoTransform()
        prj = gdal_dataset.GetProjection()

    ...or alternatively, directly from an xarray dataset:

        geotrans = xarraydataset.geobox.transform.to_gdal()
        prj = xarraydataset.geobox.crs.wkt

    Parameters
    ----------
    fname : str
        Output geotiff file path including extension
    data : numpy array
        Input array to export as a geotiff
    geo_transform : tuple
        Geotransform for output raster; e.g. "(upleft_x, x_size,
        x_rotation, upleft_y, y_rotation, y_size)"
    projection : str
        Projection for output raster (in "WKT" format)
    nodata_val : int, optional
        Value to convert to nodata in the output raster; default 0
    dtype : gdal dtype object, optional
        Optionally set the dtype of the output raster; can be
        useful when exporting an array of float or integer values.
        Defaults to gdal.GDT_Float32

    """

    # Set up driver
    driver = gdal.GetDriverByName('GTiff')

    # Create raster of given size and projection
    rows, cols = data.shape
    dataset = driver.Create(fname, cols, rows, 1, dtype)
    dataset.SetGeoTransform(geo_transform)
    dataset.SetProjection(projection)

    # Write data to array and set nodata values
    band = dataset.GetRasterBand(1)
    band.WriteArray(data)
    band.SetNoDataValue(nodata_val)

    # Close file
    dataset = None


def mostcommon_crs(dc, product, query):
    """
    Takes a given query and returns the most common CRS for observations
    returned for that spatial extent. This can be useful when your study
    area lies on the boundary of two UTM zones, forcing you to decide
    which CRS to use for your `output_crs` in `dc.load`.

    Parameters
    ----------
    dc : datacube Datacube object
        The Datacube to connect to, i.e. `dc = datacube.Datacube()`.
        This allows you to also use development datacubes if required.
    product : str
        A product name to load CRSs from
    query : dict
        A datacube query including x, y and time range to assess for the
        most common CRS

    Returns
    -------
    A EPSG string giving the most common CRS from all datasets returned
    by the query above

    """

    # List of matching products
    matching_datasets = dc.find_datasets(product=product, **query)

    # Extract all CRSs
    crs_list = [str(i.crs) for i in matching_datasets]

    # Identify most common CRS
    crs_counts = Counter(crs_list)
    crs_mostcommon = crs_counts.most_common(1)[0][0]

    # Warn user if multiple CRSs are encountered
    if len(crs_counts.keys()) > 1:

        warnings.warn(f'Multiple UTM zones {list(crs_counts.keys())} '
                      f'were returned for this query. Defaulting to '
                      f'the most common zone: {crs_mostcommon}',
                      UserWarning)

    return crs_mostcommon


def download_unzip(url,
                   output_dir=None,
                   remove_zip=True):
    """
    Downloads and unzips a .zip file from an external URL to a local
    directory.

    Parameters
    ----------
    url : str
        A string giving a URL path to the zip file you wish to download
        and unzip
    output_dir : str, optional
        An optional string giving the directory to unzip files into.
        Defaults to None, which will unzip files in the current working
        directory
    remove_zip : bool, optional
        An optional boolean indicating whether to remove the downloaded
        .zip file after files are unzipped. Defaults to True, which will
        delete the .zip file.

    """

    # Get basename for zip file
    zip_name = os.path.basename(url)

    # Raise exception if the file is not of type .zip
    if not zip_name.endswith('.zip'):
        raise ValueError(f'The URL provided does not point to a .zip '
                         f'file (e.g. {zip_name}). Please specify a '
                         f'URL path to a valid .zip file')

    # Download zip file
    print(f'Downloading {zip_name}')
    r = requests.get(url)
    with open(zip_name, 'wb') as f:
        f.write(r.content)

    # Extract into output_dir
    with zipfile.ZipFile(zip_name, 'r') as zip_ref:
        zip_ref.extractall(output_dir)
        print(f'Unzipping output files to: '
              f'{output_dir if output_dir else os.getcwd()}')

    # Optionally cleanup
    if remove_zip:
        os.remove(zip_name)


def wofs_fuser(dest, src):
    """
    Fuse two WOfS water measurements represented as `ndarray`s.

    Note: this is a copy of the function located here:
    https://github.com/GeoscienceAustralia/digitalearthau/blob/develop/digitalearthau/utils.py
    """
    empty = (dest & 1).astype(np.bool)
    both = ~empty & ~((src & 1).astype(np.bool))
    dest[empty] = src[empty]
    dest[both] |= src[both]


def dilate(array, dilation=10, invert=True):
    """
    Dilate a binary array by a specified nummber of pixels using a
    disk-like radial dilation.

    By default, invalid (e.g. False or 0) values are dilated. This is
    suitable for applications such as cloud masking (e.g. creating a
    buffer around cloudy or shadowed pixels). This functionality can
    be reversed by specifying `invert=False`.

    Parameters
    ----------
    array : array
        The binary array to dilate.
    dilation : int, optional
        An optional integer specifying the number of pixels to dilate
        by. Defaults to 10, which will dilate `array` by 10 pixels.
    invert : bool, optional
        An optional boolean specifying whether to invert the binary
        array prior to dilation. The default is True, which dilates the
        invalid values in the array (e.g. False or 0 values).

    Returns
    -------
    An array of the same shape as `array`, with valid data pixels
    dilated by the number of pixels specified by `dilation`.
    """

    y, x = np.ogrid[
        -dilation: (dilation + 1),
        -dilation: (dilation + 1),
    ]

    # disk-like radial dilation
    kernel = (x * x) + (y * y) <= (dilation + 0.5) ** 2

    # If invert=True, invert True values to False etc
    if invert:
        array = ~array

    return ~binary_dilation(array.astype(np.bool),
                            structure=kernel.reshape((1,) + kernel.shape))


def pan_sharpen_brovey(band_1, band_2, band_3, pan_band):
    '''
    Brovey pan sharpening on surface reflectance input using numexpr
    and return three xarrays.

    Parameters
    ----------
    band_1, band_2, band_3 : xarray.DataArray or numpy.array
        Three input multispectral bands, either as xarray.DataArrays or
        numpy.arrays. These bands should have already been resampled to
        the spatial resolution of the panchromatic band.
    pan_band : xarray.DataArray or numpy.array
        A panchromatic band corresponding to the above multispectral
        bands that will be used to pan-sharpen the data.

    Returns
    -------
    band_1_sharpen, band_2_sharpen, band_3_sharpen : numpy.arrays
        Three numpy arrays equivelent to `band_1`, `band_2` and `band_3`
        pan-sharpened to the spatial resolution of `pan_band`.

    '''
    # Calculate total
    exp = 'band_1 + band_2 + band_3'
    total = numexpr.evaluate(exp)

    # Perform Brovey Transform in form of: band/total*panchromatic
    exp = 'a/b*c'
    band_1_sharpen = numexpr.evaluate(exp, local_dict={'a': band_1,
                                                       'b': total,
                                                       'c': pan_band})
    band_2_sharpen = numexpr.evaluate(exp, local_dict={'a': band_2,
                                                       'b': total,
                                                       'c': pan_band})
    band_3_sharpen = numexpr.evaluate(exp, local_dict={'a': band_3,
                                                       'b': total,
                                                       'c': pan_band})

    return band_1_sharpen, band_2_sharpen, band_3_sharpen


def paths_to_datetimeindex(paths, string_slice=(0, 10)):
    '''
    Helper function to generate a Pandas datetimeindex object
    from dates contained in a file path string.

    Parameters
    ----------
    paths : list of strings
        A list of file path strings that will be used to extract times
    string_slice : tuple
        An optional tuple giving the start and stop position that
        contains the time information in the provided paths. These are
        applied to the basename (i.e. file name) in each path, not the
        path itself. Defaults to (0, 10).

    Returns
    -------
    A pandas.DatetimeIndex object containing a 'datetime64[ns]' derived
    from the file paths provided by `paths`.
    '''

    date_strings = [os.path.basename(i)[slice(*string_slice)]
                    for i in paths]
    return pd.to_datetime(date_strings)
