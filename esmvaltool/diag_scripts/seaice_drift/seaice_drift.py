"""Sea ice drift diagnostic"""
import os
import logging
import math
import csv
import calendar

import numpy as np
from scipy import stats
from matplotlib import pyplot as plt

import iris
import iris.cube
from iris.cube import Cube
import iris.analysis
import iris.analysis.cartography
import iris.coords
from iris.aux_factory import AuxCoordFactory
import pyproj
from shapely.geometry import Polygon, Point
from shapely.ops import transform
from functools import partial

import esmvaltool.diag_scripts.shared
import esmvaltool.diag_scripts.shared.names as n

logger = logging.getLogger(os.path.basename(__file__))

MONTHS_PER_YEAR = 12


class SeaIceDrift(object):

    def __init__(self, config):
        self.cfg = config
        self.datasets = esmvaltool.diag_scripts.shared.Datasets(self.cfg)
        self.variables = esmvaltool.diag_scripts.shared.Variables(self.cfg)

        self.siconc = {}
        self.sivol = {}
        self.sispeed = {}
        self.region_mask = {}

        self.slope_drift_sic = {}
        self.intercept_drift_siconc = {}
        self.slope_ratio_drift_siconc = {}
        self.error_drift_siconc = {}

        self.slope_drift_sivol = {}
        self.intercept_drift_sivol = {}
        self.slope_ratio_drift_sivol = {}
        self.error_drift_sivol = {}

    def compute(self):
        logger.info('Loading sea ice concentration')
        siconc_original = {}
        siconc_files = self.datasets.get_path_list(
            standard_name='sea_ice_area_fraction')
        for filename in siconc_files:
            reference_dataset = self._get_reference_dataset(
                self.datasets.get_info('reference_dataset', filename)
            )
            alias = self._get_alias(filename, reference_dataset)
            siconc = self._load_cube(filename, 'sea_ice_area_fraction')
            siconc.convert_units('1.0')
            siconc_original[alias] = siconc

            self.siconc[alias] = self._compute_mean(
                siconc, self._get_mask(siconc, filename)
            )

        logger.info('Loading sea ice thickness')
        sithick_files = self.datasets.get_path_list(
            standard_name='sea_ice_thickness')
        for filename in sithick_files:
            reference_dataset = self._get_reference_dataset(
                self.datasets.get_info('reference_dataset', filename)
            )
            alias = self._get_alias(filename, reference_dataset)
            sithick = self._load_cube(filename, 'sea_ice_thickness')
            if alias != 'reference':
                self.sivol[alias] = self._compute_mean(
                    sithick * siconc_original[alias],
                    self._get_mask(sithick, filename)
                )
                del sithick
            else:
                self.sivol[alias] = self._compute_mean(
                    sithick,
                    self._get_mask(sithick, filename)
                )

        logger.info('Load sea ice velocities')
        sispeed_files = self.datasets.get_path_list(
            standard_name='sea_ice_speed')
        for filename in sispeed_files:
            reference_dataset = self._get_reference_dataset(
                self.datasets.get_info('reference_dataset', filename)
            )
            alias = self._get_alias(filename, reference_dataset)
            sispeed = self._load_cube(filename, 'sea_ice_speed')
            sispeed.convert_units('km day-1')
            self.sispeed[alias] = self._compute_mean(
                sispeed, self._get_mask(sispeed, filename)
            )

        self.compute_metrics()
        self.results()
        self.save()
        self.plot_results()

    def _get_reference_dataset(self, reference_dataset):
        for filename in self.datasets:
            dataset = self.datasets.get_info(n.DATASET, filename)
            if dataset == reference_dataset:
                return filename
        raise ValueError(
            'Reference dataset "{}" not found'.format(reference_dataset)
        )

    def _get_mask(self, data, filename):
        if 'latitude_treshold' in self.cfg:
            lat_threshold = self.cfg['latitude_treshold']
            mask = data.coord('latitude').points > lat_threshold
            mask = mask.astype(np.int8)
        else:
            polygon = self.cfg['polygon']
            factory = InsidePolygonFactory(
                polygon,
                data.coord('latitude'),
                data.coord('longitude'),
            )
            data.add_aux_factory(factory)
            mask = data.coord('Inside polygon').points
            mask = mask.astype(np.int8)
            coord = data.coord('Inside polygon')
            dim_coords = data.coord_dims(coord)
            data.remove_aux_factory(factory)
            data.add_aux_coord(coord, dim_coords)
            iris.save(data, "/home/Earth/jvegas/mask.nc")
            data.remove_coord('Inside polygon')

        dataset_info = self.datasets.get_dataset_info(filename)
        area_file = dataset_info.get(n.FX_FILES, {}).get('areacello', '')
        if area_file:
            area_cello = iris.load_cube(dataset_info[n.FX_FILES]['areacello'])
        else:
            area_cello = iris.analysis.cartography.area_weights(data)

        return area_cello.data * mask

    def _load_cube(self, filepath, standard_name):
        cube = iris.load_cube(filepath, standard_name)
        cube.remove_coord('day_of_month')
        cube.remove_coord('day_of_year')
        cube.remove_coord('year')
        return cube

    def compute_metrics(self):
        for dataset in self.siconc.keys():
            logger.info('Compute diagnostics for %s', dataset)
            logger.info('Metrics drift-concentration')
            logger.info('Slope ratio (no unit)')
            slope, intercept, sd, sig = self._get_slope_ratio(
                self.siconc[dataset], self.sispeed[dataset])
            self.slope_drift_sic[dataset] = slope
            self.intercept_drift_siconc[dataset] = intercept

            logger.info('Metrics drift-thickness')
            logger.info('Slope ratio (no unit)')
            slope, intercept, sd, sig = self._get_slope_ratio(
                self.sivol[dataset], self.sispeed[dataset]
            )
            self.slope_drift_sivol[dataset] = slope
            self.intercept_drift_sivol[dataset] = intercept

        for dataset in self.siconc.keys():
            if dataset == 'reference':
                continue
            logger.info('Compute metrics for %s', dataset)
            logger.info('Compute mean errors (%)')
            self.error_drift_siconc[dataset] = self._compute_error(
                self.siconc[dataset], self.siconc['reference'],
                self.sispeed[dataset], self.siconc['reference']
            )
            self.error_drift_sivol[dataset] = self._compute_error(
                self.sivol[dataset], self.sivol['reference'],
                self.sispeed[dataset], self.siconc['reference']
            )

            logger.info('Compute relative slope ratios ')
            self.slope_ratio_drift_siconc[dataset] = \
                self.slope_drift_sic[dataset] / \
                self.slope_drift_sic['reference']
            self.slope_ratio_drift_sivol[dataset] = \
                self.slope_drift_sivol[dataset] / \
                self.slope_drift_sivol['reference']

    def _get_alias(self, filename, reference_dataset):
        filename = self._get_alias_name(filename)
        reference_dataset = self._get_alias_name(reference_dataset)
        if filename == reference_dataset:
            return 'reference'
        else:
            return filename

    def _get_alias_name(self, filename):
        info = self.datasets.get_dataset_info(filename)

        if info[n.PROJECT] == 'OBS':
            temp = '{project}_{dataset}_{start}_{end}'
        else:
            temp = '{project}_{dataset}_{experiment}_{ensemble}_{start}_{end}'

        return temp.format(
            project=info[n.PROJECT],
            dataset=info[n.DATASET],
            experiment=info.get(n.EXP, ''),
            ensemble=info.get(n.ENSEMBLE, ''),
            start=info[n.START_YEAR],
            end=info[n.END_YEAR],
        )

    def _compute_mean(self, data, weights):
        domain_mean = iris.cube.CubeList()
        model_cube = None
        for map_slice in data.slices_over('time'):
            mean = np.average(map_slice.data, weights=weights)
            if not model_cube:
                cube = Cube(
                    mean,
                    standard_name=map_slice.standard_name,
                    var_name=map_slice.var_name,
                    long_name=map_slice.long_name,
                    units=map_slice.units,
                    attributes=map_slice.attributes.copy(),
                )
                model_cube = cube
            cube = model_cube.copy(mean)
            cube.add_aux_coord(map_slice.coord('time'))
            cube.add_aux_coord(map_slice.coord('month_number'))
            domain_mean.append(cube)
        domain_mean = domain_mean.merge_cube()
        return domain_mean.aggregated_by('month_number', iris.analysis.MEAN)

#     def _load_IABP_bouys(self):
#         logger.info('Sea ice drift (IABP buoys)')
#         if not self.recalculate and os.path.isfile(self.drift_IABP_file):
#             self.drift_IABP[SCICEX] = iris.load_cube(self.drift_IABP_file,
#                                                      'Drift mean over '
#                                                      'SCICEX domain')
#             self.drift_IABP[LAT] = iris.load_cube(self.drift_IABP_file,
#                                                   'Drift mean over latitude '
#                                                   'threshold')
#             return
#
#         # List sea ice drift speed data files -
#         # retrieved from NSIDC (https://nsidc.org/data/NSIDC-0116)
#         filelist = glob.glob(os.path.join(self.dir_iabp, '*v3.txt'))
#         filelist.sort()
#         n_files = len(filelist)  # number of files
#
#         # Check for empty files
#         def checkIfEmpty(fname, header_cutoff):
#             return os.path.getsize(fname) < header_cutoff
#
        # # Read EASE-Grid file - retrieved from NSIDC
        # # (https://nsidc.org/data/NSIDC-0116)
        # grid_file_path = os.path.join(self.dir_iabp, 'north_x_y_lat_lon.txt')
        # x_grid, y_grid, lat_grid, lon_grid = np.loadtxt(grid_file_path,
        #                                                 usecols=(0, 1, 2, 3),
        #                                                 unpack=True)

        # # Longitude and latitude of SCICEX vertices
        # lon_scicex = np.array((-15., -60., -130., -141, -141, -155, 175,
        #                        172, 163, 126, 110, 80, 57, 33, 8),
        #                       dtype=float)
        # lat_scicex = np.array((87, 86.58, 80, 80, 70, 72, 75.5, 78.5,
        #                        80.5, 78.5, 84.33, 84.42, 85.17, 83.8,
        #                        84.08),
        #                       dtype=float)

#         # Find closest EASE-Grid x and y coordinates of SCICEX vertices
#         x_scicex = np.zeros(15)
#         y_scicex = np.zeros(15)
#         # parameter to take a range around latitude and longitude
#         variation = 0.5
#         for j_scicex in np.arange(len(lon_scicex)):
#             lon0 = lon_scicex[j_scicex] + 2 * variation
#             lat0 = lat_scicex[j_scicex] + 2 * variation
#             for j in np.arange(len(x_grid)):
#                 if (lon_scicex[j_scicex] - variation) <= lon_grid[j] <= \
#                     (lon_scicex[j_scicex] + variation) and \
#                     (lat_scicex[j_scicex] - variation) <= lat_grid[j] <= \
#                     (lat_scicex[j_scicex] + variation):
#                     lon1 = lon_grid[j]
#                     lat1 = lat_grid[j]
#                     if np.abs(lon1 - lon_scicex[j_scicex]) < \
#                         np.abs(lon0 - lon_scicex[j_scicex]) and \
#                         np.abs(lat1 - lat_scicex[j_scicex]) < \
#                         np.abs(lat0 - lat_scicex[j_scicex]):
#                         x_scicex[j_scicex] = x_grid[j]
#                         y_scicex[j_scicex] = y_grid[j]
#                         lon0 = lon_grid[j]
#                         lat0 = lat_grid[j]
#
#         # Put SCICEX vertices (EASE-Grid x and y) into a path
#         scicex_box = matplotlib.path.Path(
#             [(x_scicex[0], y_scicex[0]), (x_scicex[1], y_scicex[1]),
#              (x_scicex[2], y_scicex[2]), (x_scicex[3], y_scicex[3]),
#              (x_scicex[4], y_scicex[4]), (x_scicex[5], y_scicex[5]),
#              (x_scicex[6], y_scicex[6]), (x_scicex[7], y_scicex[7]),
#              (x_scicex[8], y_scicex[8]), (x_scicex[9], y_scicex[9]),
#              (x_scicex[10], y_scicex[10]), (x_scicex[11], y_scicex[11]),
#              (x_scicex[12], y_scicex[12]), (x_scicex[13], y_scicex[13]),
#              (x_scicex[14], y_scicex[14])])
#
#         # Find corners (xmin, xmax, ymin, ymax) of domain north
#         # of latitude threshold
#         indexes = np.where(lat_grid >= self.lat_threshold)
#         xmin = np.min(x_grid[indexes])
#         xmax = np.max(x_grid[indexes])
#
#         ymin = np.min(y_grid[indexes])
#         ymax = np.max(y_grid[indexes])
#
#         # Compute multi-year mean sea ice drift averaged over whole domain
#         # daily mean over SCICEX domain
#         drift_s = np.zeros(n_files)
#         # daily mean over domain north of latitude threshold
#         drift_l = np.zeros(n_files)
#         k = 0
#         # multi-year monthly mean over SCICEX domain
#         drift_scicex = np.zeros(MONTHS_PER_YEAR)
#         # multi-year monthly mean over domain north of latitude threshold
#         drift_lat = np.zeros(MONTHS_PER_YEAR)
#         # number of observations to compute drift_scicex
#         n_scicex = np.zeros(MONTHS_PER_YEAR)
#         # number of observations to compute drift_lat
#         n_lat = np.zeros(MONTHS_PER_YEAR)
#         scicex_index = []
#         lat_index = []
#
#         for i in filelist:
#             if not checkIfEmpty(i, 15):
#                 number = os.path.basename(i)
#                 number = re.sub('icemotion[.]vect[.]buoy[.]([0-9]+)'
#                                 '[.]n[.]v3[.]txt$', r'\1', number)
#                 date = datetime.datetime.strptime(number, '%Y%j')
#                 month_index = date.month - 1
#                 x, y, u, v, t = np.loadtxt(i, skiprows=1, usecols=range(5),
#                                            unpack=True)
#                 if len(np.atleast_1d(x)) > 1:
#                     drift = np.sqrt(u ** 2. + v ** 2.) * 86400. / 100000
#                     n_s = 0  # number of observations to compute drift_s
#                     n_l = 0  # number of observations to compute drift_l
#                     for j in np.arange(len(x)):
#                         # SCICEX box
#                         if scicex_box.contains_points([(x[j] / 5, y[j] /
#                                                                   5)]):
#                             drift_s[k] = drift_s[k] + drift[j]
#                             n_s += + 1
#                             scicex_index.append((k, j))
#                         # Domain north of latitude threshold
#                         if xmin <= round(x[j] / 5) <= xmax and \
#                             ymin <= round(y[j] / 5) <= ymax:
#                             drift_l[k] = drift_l[k] + drift[j]
#                             n_l += 1
#                             lat_index.append((k, j))
#                     if n_s > 0:
#                         drift_s[k] = drift_s[k] / n_s
#                     else:
#                         drift_s[k] = np.nan
#                     if n_l > 0:
#                         drift_l[k] = drift_l[k] / n_l
#                     else:
#                         drift_l[k] = np.nan
#                     if not np.isnan(drift_s[k]):
#                         drift_scicex[month_index] = \
#                             drift_scicex[month_index] + drift_s[k]
#                         n_scicex[month_index] += 1
#                     if not np.isnan(drift_l[k]):
#                         drift_lat[month_index] = drift_lat[month_index] + \
#                                                  drift_l[k]
#                         n_lat[month_index] += 1
#             k += 1
#
#         drift_scicex = np.divide(drift_scicex, n_scicex)
#         drift_lat = np.divide(drift_lat, n_lat)
#
#         drift_scicex = iris.cube.Cube(drift_scicex,
#                                       long_name="Drift mean over SCICEX "
#                                                 "domain",
#                                       var_name="scicex_drift",
#                                       units='km day-1')
#         drift_scicex.add_dim_coord(
#             iris.coords.DimCoord(range(1, 13), var_name='month_number',
#                                  long_name='month_number', units='1'), 0)
#
#         drift_lat = iris.cube.Cube(drift_lat,
#                                    long_name="Drift mean over latitude "
#                                              "threshold",
#                                    var_name="lat_drift",
#                                    units='km day-1')
#         drift_lat.attributes['lat_threshold'] = self.lat_threshold
#         drift_lat.add_dim_coord(
#             iris.coords.DimCoord(range(1, 13), var_name='month_number',
#                                  long_name='month_number', units='1'), 0)
#         iris.save((drift_scicex, drift_lat), self.drift_IABP_file, zlib=True)
#         self.drift_IABP[SCICEX] = drift_scicex
#         self.drift_IABP[LAT] = drift_lat
#
#     def _load_osisaf_sic(self):
#         logger.info('Sea ice concentration (OSI SAF)')
#
#         if self.recalculate or not os.path.isfile(self.siconc_OSISAF_file):
#             fname = 'siconc_OImon_OSISAF_10km*.nc'
#
#             with iris.FUTURE.context(cell_datetime_objects=True):
#                 cubes = iris.load(os.path.join(self.dir_osisaf, fname),
#                                   self.years_constraint &
#                                   iris.Constraint(name=
#                                                   'sea_ice_area_fraction'))
#             equalise_attributes(cubes)
#             iris.util.unify_time_units(cubes)
#             cube = cubes.concatenate_cube()
#             iris.coord_categorisation.add_day_of_year(cube, 'time')
#             iris.coord_categorisation.add_year(cube, 'time')
#             iris.coord_categorisation.add_month_number(cube, 'time')
#             tmp_file = os.path.join(self.dir, 'tmp.nc')
#             iris.save(cube, tmp_file, zlib=True)
#             Cdo().remapbil(self.models[0].siconc_file, input=tmp_file,
#                            output=self.siconc_OSISAF_file)
#             os.remove(tmp_file)
#
#         self.siconc_OSISAF[RAW] = iris.load_cube(self.siconc_OSISAF_file,
#                                                  'sea_ice_area_fraction')
#         return
#
#     def _load_piomas(self):
#         logger.info('Sea ice thickness (PIOMAS)')
#         if not self.recalculate and os.path.isfile(self.sivol_PIOMAS_file):
#             self.sivol_PIOMAS[SCICEX] = iris.load_cube(
#                 self.sivol_PIOMAS_file,
#                 'Sea ice volume mean over SCICEX domain')
#             self.sivol_PIOMAS[LAT] = iris.load_cube(
#                 self.sivol_PIOMAS_file,
#                 'Sea ice volume mean over latitude threshold')
#             return
#         filelist = glob.glob(os.path.join(self.dir_piomas, 'heff.txt*'))
#         # multi-year monthly mean thickness over SCICEX domain
#         sithic = np.zeros(MONTHS_PER_YEAR)
#         # multi-year monthly mean thickness over domain north of threshold
#         h_lat = np.zeros(MONTHS_PER_YEAR)
#         h = [None] * MONTHS_PER_YEAR
#         nyears = 0
#         for i in sorted(filelist):
#             year, lat, lon, h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], \
#             h[8], h[9], h[10], h[11] = np.loadtxt(i, unpack=True)
#             if self.start_year <= year[0] <= self.end_year:
#                 mask_scicex = self.scicex_domain_1d(lon, lat)
#                 mask_lat = self.lat_domain(lat)
#                 n_s = 0  # number of observations for SCICEX domain
#                 # monthly mean thickness over SCICEX domain
#                 h_s = np.zeros(MONTHS_PER_YEAR)
#                 # monthly mean thickness over domain north of
#                 # latitude threshold
#                 h_l = np.zeros(MONTHS_PER_YEAR)
#                 # number of observations for domain north of
#                 # latitude threshold
#                 n_l = 0
#                 for j in np.arange(len(year)):
#                     if mask_scicex[j]:
#                         for x in range(MONTHS_PER_YEAR):
#                             h_s[x] += h[x][j]
#                         n_s = n_s + 1
#                     if mask_lat[j]:
#                         for x in range(MONTHS_PER_YEAR):
#                             h_l[x] += h[x][j]
#                         n_l = n_l + 1
#                 sithic += (h_s/n_s)
#                 h_lat += (h_l/n_l)
#                 nyears += 1
#         sithic /= nyears
#         h_lat /= nyears
#
#         self.sivol_PIOMAS[SCICEX] = iris.cube.Cube(sithic,
#                                                    long_name="Sea ice "
#                                                              "volume mean
#                                                              "over "
#                                                              "SCICEX domain",
#                                                    var_name="scicex_sivol",
#                                                    units='m')
#         self.sivol_PIOMAS[SCICEX].add_dim_coord(
#             iris.coords.DimCoord(range(1, 13), var_name='month_number',
#                                  long_name='month_number', units='1'), 0)
#
#         self.sivol_PIOMAS[LAT] = iris.cube.Cube(h_lat,
#                                                 long_name="Sea ice volume "
#                                                           "mean over "
#                                                           "latitude "
#                                                           "threshold",
#                                                 var_name="lat_sivol",
#                                                 units='m')
#         self.sivol_PIOMAS[LAT].attributes['lat_threshold'] = \
#             self.lat_threshold
#         self.sivol_PIOMAS[LAT].add_dim_coord(
#             iris.coords.DimCoord(range(1, 13), var_name='month_number',
#                                  long_name='month_number', units='1'), 0)
#         iris.save((self.sivol_PIOMAS[SCICEX], self.sivol_PIOMAS[LAT]),
#                   self.sivol_PIOMAS_file, zlib=True)
#
#     def lat_domain(self, lat):
#         return lat >= self.lat_threshold
#
#     def scicex_domain(self, lon, lat):
#         self._prepare_scicex_box()
#
#         x, y = self.map(lon, lat)
#         # Check which model grid points fall into domain and create mask
#         # (1: inside domain; 0: outside domain)
#         mask_scicex = np.zeros(shape=lon.shape)
#         for jx in np.arange(lon.shape[0]):
#             for jy in np.arange(lon.shape[1]):
#                 if self.scicex_box.contains_points([(x[jx, jy], y[jx, jy])]):
#                     mask_scicex[jx, jy] = 1
#         return mask_scicex
#
#     def scicex_domain_1d(self, lon, lat):
#         # lon: longitude
#         # lat: latitude
#         self._prepare_scicex_box()
#         n = len(lon)
#         x, y = self.map(lon, lat)
#
#         # Check which model grid points fall into domain and create mask
#         # (1: inside domain; 0: outside domain)
#         mask_scicex = np.zeros(n)
#         for j in range(n):
#             if self.scicex_box.contains_points([(x[j], y[j])]):
#                 mask_scicex[j] = 1
#
#         # Return value of mask
#         return mask_scicex
#
#     def _prepare_scicex_box(self):
#         if self.scicex_box is not None:
#             return
#         # Store SCICEX vertices (lon,lat)
#         scicex_vertices = (
#             (-15, 87), (-60, 86.58), (-130, 80), (-141, 80), (-141, 70),
#             (-155, 72), (175, 75.5), (172, 78.5),(163, 80.5),(126, 78.5),
#             (110, 84.33), (80, 84.42), (57, 85.17), (33, 83.8), (8, 84.08))
#         # Map projection
#         boundlat = 50.
#         l0 = 0.
#         self.map = Basemap(projection='nplaea', boundinglat=boundlat,
#                            lon_0=l0, resolution='c')
#         path_list = [self.map(i[0], i[1]) for i in scicex_vertices]
#         self.scicex_box = matplotlib.path.Path(path_list)
#
#     def compute_monthly_mean_over_domain(self):
#         logger.info('Compute monthly mean averaged over whole domain')
#         logger.info('Models')
#         for model in self.models:
#             self._average_model_variables(model)
#         self._average_osisaf()
#
#     def _average_osisaf(self):
#         logger.info('OSISAF averages')
#         siconc_raw = self.siconc_OSISAF[RAW]
#         lon = siconc_raw.coord('longitude').points
#         lat = siconc_raw.coord('latitude').points
#         grid_area = self._get_grid_area(self.models[0])
#         mask_scicex = (siconc_raw.data >=
#                        self.siconc_threshold[SCICEX]) * \
#                       self.scicex_domain(lon, lat) * grid_area
#         mask_lat = (siconc_raw.data >=
#                     self.siconc_threshold[LAT]) * \
#                    self.lat_domain(lat) * grid_area
#         self.siconc_OSISAF[SCICEX] = \
#             siconc_raw.collapsed(['latitude','longitude'],
#                                  iris.analysis.MEAN,
#                                  weights=mask_scicex)
#         self.siconc_OSISAF[LAT] = \
#             siconc_raw.collapsed(['latitude','longitude'],
#                                  iris.analysis.MEAN,
#                                  weights=mask_lat)
#         del siconc_raw
#
#     def _average_model_variables(self, model):
#         logger.info(model.name)
#         lon = model.siconc[RAW].coord('longitude').points
#         lat = model.siconc[RAW].coord('latitude').points
#
#         for dom in DOMAINS:
#             if dom == SCICEX:
#                 mask = self.scicex_domain(lon, lat)
#             else:
#                 mask = self.lat_domain(lat)
#             grid_area = self._get_grid_area(model)
#             mask = mask * grid_area * (model.siconc[RAW].data >=
#                                        self.siconc_threshold[dom])
#
#             model.drift[dom] = self._compute_domain_mean(model.drift[RAW],
#                                                          mask)
#             model.siconc[dom] = self._compute_domain_mean(model.siconc[RAW],
#                                                           mask)
#             model.sivol[dom] = self._compute_domain_mean(model.sivol[RAW],
#                                                          mask)
#         del model.siconc[RAW]
#         del model.drift[RAW]
#         del model.sivol[RAW]
#
#     def _get_grid_area(self, model):
#         # Compute area of grid cells from grid file
#         mesh_file = os.path.join(model.path, model.mesh_file)
#         e1t = iris.load_cube(mesh_file, 'e1t')
#         e2t = iris.load_cube(mesh_file, 'e1t')
#         grid_area = e1t * e2t
#         grid_area.add_dim_coord(iris.coords.DimCoord([0], 'time'), 0)
#         grid_area = grid_area.collapsed('time', iris.analysis.MEAN)
#         return grid_area.data
#
#     def _compute_domain_mean(self, data, mask):
#         return data.collapsed(['latitude', 'longitude'], iris.analysis.MEAN,
#                               weights=mask)
#
#     def compute_multi_year_monthly_mean(self):
#         logger.info('Compute multi-year monthly mean averaged over '
#                          'whole domain')
#
#         for model in self.models:
#             for domain in DOMAINS:
#                 model.drift[domain] = self._multiyear_mean(
#                     model.drift[domain])
#                 model.siconc[domain] = self._multiyear_mean(
#                     model.siconc[domain])
#                 model.sivol[domain] = self._multiyear_mean(
#                     model.sivol[domain])
#
#         for domain in DOMAINS:
#             self.drift_IABP[domain] = self._multiyear_mean(
#                 self.drift_IABP[domain])
#             self.siconc_OSISAF[domain] = self._multiyear_mean(
#                 self.siconc_OSISAF[domain])
#             self.sivol_PIOMAS[domain] = self._multiyear_mean(
#                 self.sivol_PIOMAS[domain])
#
#     def _multiyear_mean(self, data):
#         try:
#             data.coord('month_number')
#         except iris.exceptions.CoordinateNotFoundError:
#             iris.coord_categorisation.add_month_number(data, 'time')
#         return data.aggregated_by('month_number', iris.analysis.MEAN)
#
#     def compute_metrics(self):
#         for model in self.models:
#             logger.info('Compute metrics for {0}'.format(model.name))
#             for domain in DOMAINS:
#                 logger.info('Domain {0}'.format(domain))
#                 logger.info('Metrics drift-concentration')
#                 logger.info('Slope ratio (no unit)')
#                 slope, intercept, sd, sig = self._get_slope_ratio(
#                     model.siconc[domain], model.drift[domain])
#                 slope_obs, intercept_obs, sd_obs, sig_obs = \
#                     self._get_slope_ratio(self.siconc_OSISAF[domain],
#                                           self.drift_IABP[domain])
#                 model.slope_drift_siconc[domain] = slope
#                 model.intercept_drift_siconc[domain] = intercept
#                 self.slope_drift_siconc[domain] = slope_obs
#                 self.intercept_drift_siconc[domain] = intercept_obs
#                 model.slope_ratio_drift_siconc[domain] = slope / slope_obs
#
#                 logger.info('Mean error (%)')
#                 model.error_drift_siconc[domain] = \
#                     self._compute_error(model.siconc[domain],
#                                         self.siconc_OSISAF[domain],
#                                         model.drift[domain],
#                                         self.drift_IABP[domain])
#
#                 logger.info('Metrics drift-thickness')
#                 logger.info('Slope ratio (no unit)')
#
#                 slope, intercept, sd, sig = \
#                     self._get_slope_ratio(model.sivol[domain],
#                                           model.drift[domain])
#                 slope_obs, intercept_obs, sd_obs, sig_obs = \
#                     self._get_slope_ratio(self.sivol_PIOMAS[domain],
#                                           self.drift_IABP[domain])
#
#                 model.slope_drift_sivol[domain] = slope
#                 model.intercept_drift_sivol[domain] = intercept
#                 self.slope_drift_sivol[domain] = slope_obs
#                 self.intercept_drift_sivol[domain] = intercept_obs
#                 model.slope_ratio_drift_sivol[domain] = slope / slope_obs
#
#                 logger.info('Mean error (%)')
#                 model.error_drift_sivol[domain] = \
#                     self._compute_error(model.sivol[domain],
#                                         self.sivol_PIOMAS[domain],
#                                         model.drift[domain],
#                                         self.drift_IABP[domain])
#

    def _compute_error(self, var, var_obs, drift, drift_obs):
        var = var.data
        var_obs = var_obs.data
        drift = drift.data
        drift_obs = drift_obs.data
        return 100. * np.nanmean((np.absolute(var - var_obs) /
                                  np.nanmean(var_obs)) ** 2. +
                                 (np.absolute(drift - drift_obs) /
                                  np.nanmean(drift_obs)) ** 2.)

    def _get_slope_ratio(self, siconc, drift):
        slope, intercept = np.polyfit(siconc.data, drift.data, 1)
        sd, sig = self._sd_slope(slope, intercept, siconc.data, drift.data)
        return slope, intercept, sd, sig

    def _sd_slope(self, slope, intercept, sivar, drift):
        # Parameters
        alpha = 0.05  # significance level
        nfreedom = MONTHS_PER_YEAR - 2  # number of degrees of freedom
        t_crit = stats.t.ppf(1 - alpha / 2, nfreedom)  # critical Student's t

        # Compute standard deviation of slope
        lreg = slope * sivar + intercept  # linear regression
        s_yx = np.sum((drift - lreg) ** 2) / (MONTHS_PER_YEAR - 2)
        SS_xx = np.sum((sivar - np.mean(sivar)) ** 2)
        sd_slope = np.sqrt(s_yx / SS_xx)  # Standard deviation of slope

        # Significance
        ta = slope / sd_slope  # Student's t
        sig_slope = 0
        if np.abs(ta) > t_crit:
            sig_slope = 1

        # Return value of mask
        return sd_slope, sig_slope

    def results(self):
        logger.info('Results')
        for model in self.siconc.keys():
            self._print_results(model)

    def _print_results(self, model):
        if model == 'reference':
            return
        logger.info('Dataset {0}'.format(model))
        if 'latitude_treshold' in self.cfg:
            logger.info(
                'Metrics computed over domain north of %s',
                self.cfg['latitude_treshold']
            )
        else:
            logger.info(
                'Metrics computed inside %s region',
                self.cfg.get('polygon_name', 'SCICEX')
            )
        logger.info('Slope ratio Drift-Concentration = {0:.3}'
                    ''.format(self.slope_ratio_drift_siconc[model]))
        logger.info('Mean error Drift-Concentration (%) = {0:.4}'
                    ''.format(self.error_drift_siconc[model]))
        logger.info('Slope ratio Drift-Thickness = {0:.3}'.format(
            self.slope_ratio_drift_sivol.get(model, math.nan)
        ))
        logger.info('Mean error Drift-Thickness (%) = {0:.4}'
                    ''.format(self.error_drift_sivol.get(model, math.nan)))

    def save(self):
        if not True:
            return

        logger.info('Save variables')
        for dataset in self.siconc.keys():
            self.save_climatologies(dataset)
            self.save_slope(dataset)

    def save_climatologies(self, dataset):
        base_path = os.path.join(self.cfg[n.WORK_DIR], dataset)
        if not os.path.isdir(base_path):
            os.makedirs(base_path)
        iris.save(
            self.sispeed[dataset],
            os.path.join(
                base_path,
                'sispeed_clim.nc'
            ),
            zlib=True
        )
        iris.save(
            self.siconc[dataset],
            os.path.join(
                base_path,
                'siconc_clim.nc'
            ),
            zlib=True
        )
        iris.save(
            self.sivol[dataset],
            os.path.join(
                base_path,
                'sivol_clim.nc'
            ),
            zlib=True
        )

    def save_slope(self, dataset):
        base_path = os.path.join(self.cfg[n.WORK_DIR], dataset)
        if not os.path.isdir(base_path):
            os.makedirs(base_path)
        siconc_path = os.path.join(base_path, 'metric_drift_siconc.csv')
        sivol_path = os.path.join(base_path, 'metric_drift_sivol.csv')
        with open(siconc_path, 'w') as csvfile:
            csv_writer = csv.writer(csvfile)
            csv_writer.writerow(('slope', 'intercept',
                                 'slope_ratio', 'error'))
            csv_writer.writerow((
                self.slope_drift_sic[dataset],
                self.intercept_drift_siconc[dataset],
                self.slope_ratio_drift_siconc.get(dataset, None),
                self.error_drift_siconc.get(dataset, None)
            ))

        with open(sivol_path, 'w') as csvfile:
            csv_writer = csv.writer(csvfile)
            csv_writer.writerow(('slope', 'intercept',
                                 'slope_ratio', 'error'))
            csv_writer.writerow((
                self.slope_drift_sivol[dataset],
                self.intercept_drift_sivol[dataset],
                self.slope_ratio_drift_sivol.get(dataset, None),
                self.error_drift_sivol.get(dataset, None)
            ))

    def plot_results(self):
        if not self.cfg[n.WRITE_PLOTS]:
            return
        logger.info('Plotting results')
        for model in self.siconc.keys():
            if model == 'reference':
                continue
            logger.info('Results for {0}'.format(model))
            self._plot_domain(model)

    def _plot_domain(self, dataset):
        fig, ax = plt.subplots(1, 2, figsize=(18, 6))
        self._plot_drift_siconc(ax[0], dataset)
        self._plot_drift_sivol(ax[1], dataset)
        base_path = os.path.join(self.cfg[n.PLOT_DIR], dataset)
        if not os.path.isdir(base_path):
            os.makedirs(base_path)
        fig.savefig(os.path.join(
            base_path,
            'drift-strength.{0}'.format(self.cfg[n.OUTPUT_FILE_TYPE])
            )
        )

    def _plot_drift_sivol(self, ax, dataset):

        drift = self.sispeed[dataset].data
        sivol = self.sivol[dataset].data

        slope_sivol = self.slope_drift_sivol[dataset]
        slope_ratio_sivol = self.slope_ratio_drift_sivol[dataset]
        intercept_sivol = self.intercept_drift_sivol[dataset]
        error_sivol = self.error_drift_sivol[dataset]

        slope_sivol_obs = self.slope_drift_sivol[dataset]
        intercept_sivol_obs = self.intercept_drift_sivol[dataset]

        drift_obs = self.sispeed['reference'].data
        sivol_obs = self.sivol['reference'].data

        ax.plot([sivol[-1], sivol[0]], [drift[-1], drift[0]], 'r-',
                linewidth=2)
        ax.plot(sivol, drift, 'ro-', label=dataset, linewidth=2)
        ax.plot(sivol, slope_sivol * sivol + intercept_sivol, 'r:',
                linewidth=2)

        ax.plot([sivol_obs[-1], sivol_obs[0]], [drift_obs[-1], drift_obs[0]],
                'b-', linewidth=2)
        ax.plot(
            sivol_obs,
            drift_obs,
            'bo-',
            label=r'reference volume / speed ($s_h$=' +
                  str(np.round(slope_ratio_sivol, 1)) +
                  r'; $\epsilon_h$=' +
                  str(np.round(error_sivol, 1)) +
                  r'$\%$)',
            linewidth=2
        )
        ax.plot(sivol_obs, slope_sivol_obs * sivol_obs + intercept_sivol_obs,
                'b:', linewidth=2)
        ax.legend(loc='lower right', shadow=True, frameon=False, fontsize=12)
        ax.set_xlabel('Sea ice thickness (m)', fontsize=18)
        ax.set_ylabel('Sea ice drift speed (km d$^{-1}$)', fontsize=18)
        ax.tick_params(axis='both', labelsize=14)
        high_sivol, low_sivol = self._get_plot_limits(sivol, sivol_obs)
        high_drift, low_drift = self._get_plot_limits(drift, drift_obs)
        ax.axis([low_sivol, high_sivol, low_drift, high_drift])
        self._annotate_points(ax, sivol, drift)
        self._annotate_points(ax, sivol_obs, drift_obs)
        ax.grid()
        ax.set_title('Seasonal cycle {0}'.format(dataset),
                     fontsize=18)

    def _plot_drift_siconc(self, ax, dataset):
        drift = self.sispeed[dataset].data
        siconc = self.siconc[dataset].data

        slope_siconc = self.slope_drift_sic[dataset]
        slope_ratio_siconc = self.slope_ratio_drift_sivol[dataset]
        intercept_siconc = self.intercept_drift_siconc[dataset]
        error_siconc = self.error_drift_siconc[dataset]

        slope_siconc_obs = self.slope_drift_sic[dataset]
        intercept_siconc_obs = self.intercept_drift_siconc[dataset]

        drift_obs = self.sispeed['reference'].data
        siconc_obs = self.siconc['reference'].data

        ax.plot(siconc, drift, 'ro-', label=dataset)
        ax.plot(siconc, slope_siconc * siconc + intercept_siconc, 'r:',
                linewidth=2)
        ax.plot(
            siconc_obs,
            drift_obs,
            'bo-',
            label=r'reference ($s_A$=' +
                  str(np.round(slope_ratio_siconc, 1)) +
                  r'; $\epsilon_A$=' + str(np.round(error_siconc, 1)) +
                  r'$\%$)'
        )
        ax.plot(siconc_obs, slope_siconc_obs * siconc_obs +
                intercept_siconc_obs,
                'b:', linewidth=2)
        ax.legend(loc='lower left', shadow=True, frameon=False, fontsize=12)
        ax.set_xlabel('Sea ice concentration', fontsize=18)
        ax.set_ylabel('Sea ice drift speed (km d$^{-1}$)', fontsize=18)
        ax.tick_params(axis='both', labelsize=14)
        high_drift, low_drift = self._get_plot_limits(drift, drift_obs)
        ax.axis([0., 1, low_drift, high_drift])
        self._annotate_points(ax, siconc, drift)
        self._annotate_points(ax, siconc_obs, drift_obs)
        ax.grid(linewidth=0.01)
        ax.set_title('Seasonal cycle {0}'.format(dataset),
                     fontsize=18)

    def _annotate_points(self, ax, xvalues, yvalues):
        for x, y, z in zip(xvalues, yvalues, range(1, 12 + 1)):
            ax.annotate(calendar.month_abbr[z][0], xy=(x, y), xytext=(10, 5),
                        ha='right', textcoords='offset points')

    def _get_plot_limits(self, sivol, sivol_obs):
        low = min(min(sivol), min(sivol_obs)) - 0.4
        low = 0.5 * math.floor(2.0 * low)
        high = max(max(sivol), max(sivol_obs)) + 0.4
        high = 0.5 * math.ceil(2.0 * high)
        return high, low


class InsidePolygonFactory(AuxCoordFactory):
    """Defines a coordinate """
    def __init__(self, polygon=None, lat=None, lon=None):
        """
        Args:
        * polygon: List
            List of (lon, lat) tuples defining the polygon
        * lat: Coord
            The coordinate providing the latitudes.
        * lon: Coord
            The coordinate providing the longitudes.
        """
        super(InsidePolygonFactory, self).__init__()

        self._project = partial(
            pyproj.transform,
            pyproj.Proj(
                '+proj=merc +a=6378137 +b=6378137 +lat_ts=0.0'
                '+lon_0=0.0 +x_0=0.0'
                ' +y_0=0 +k=1.0 +units=m +nadgrids=@null +no_defs'
            ),
            pyproj.Proj(init='epsg:4326'),
        )

        self.polygon = transform(self._project, Polygon(polygon))
        self.lat = lat
        self.lon = lon

        self.standard_name = None
        self.long_name = 'Inside polygon'
        self.var_name = 'inpoly'
        self.units = '1.0'
        self.attributes = {}

    @property
    def dependencies(self):
        """
        Returns a dictionary mapping from constructor argument names to
        the corresponding coordinates.
        """
        return {'lat': self.lat, 'lon': self.lon}

    def _derive(self, lat, lon):
        def in_polygon(lat, lon):
            if lon > 180:
                lon -= 360
            elif lon < -180:
                lon += 360
            point = transform(self._project, Point(lon, lat))
            return 1. if self.polygon.contains(point) else np.nan
        vectorized = np.vectorize(in_polygon)
        return vectorized(lat, lon)

    def make_coord(self, coord_dims_func):
        """
        Returns a new :class:`iris.coords.AuxCoord` as defined by this
        factory.
        Args:
        * coord_dims_func:
            A callable which can return the list of dimensions relevant
            to a given coordinate.
            See :meth:`iris.cube.Cube.coord_dims()`.
        """
        # Which dimensions are relevant?
        derived_dims = self.derived_dims(coord_dims_func)
        dependency_dims = self._dependency_dims(coord_dims_func)

        # Build the points array.
        nd_points_by_key = self._remap(dependency_dims, derived_dims)
        points = self._derive(nd_points_by_key['lat'],
                              nd_points_by_key['lon'],)

        bounds = None

        in_polygon = iris.coords.AuxCoord(
            points,
            standard_name=self.standard_name,
            long_name=self.long_name,
            var_name=self.var_name,
            units=self.units,
            bounds=None,
            attributes=self.attributes,
            coord_system=self.coord_system
        )
        return in_polygon

    def update(self, old_coord, new_coord=None):
        """
        Notifies the factory of the removal/replacement of a coordinate
        which might be a dependency.
        Args:
        * old_coord:
            The coordinate to be removed/replaced.
        * new_coord:
            If None, any dependency using old_coord is removed, otherwise
            any dependency using old_coord is updated to use new_coord.
        """
        if self.lat is old_coord:
            self.lat = new_coord
        elif self.lon is old_coord:
            self.lon = new_coord


if __name__ == '__main__':
    with esmvaltool.diag_scripts.shared.run_diagnostic() as config:
        SeaIceDrift(config).compute()