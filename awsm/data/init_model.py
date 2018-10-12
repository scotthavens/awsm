from smrf import ipw
import numpy as np
import os
import logging
import netCDF4 as nc
from datetime import timedelta

C_TO_K = 273.16
FREEZE = C_TO_K

"""
Outline

-get_init_file:
    --reads in the specific init file and stores init fields in dictionary
    --checks if restarting from a crash, in which casse init file is not used
    --if running isnobal and init file is ipw then just passes fp to init
    --if no file then make the necessary 0 start file

-write_init:
    --make the needed file or datatype to init the runs
    --if isnobal then write the file or just pass the fp if input was pw init
    --make init dictionary to pass to any pysnobal

-make_backup:
    -- backup init state in netcdf file

"""

class modelInit():
    """
    Class for initializing snow model. Only runs if a model is specified
    in the AWSM config.

    Attributes:
        init:       Dictionary of init fields
        fp_init:    File pointer if iSnobal init file


    """

    def __init__(self, logger, cfg, topo, start_wyhr, pathro,
                 pathinit, wy_start):
        """
        Args:
            logger:         AWSM logger
            cfg:            AWSM config dictionary
            topo:           AWSM topo class
            start_wyhr:     WYHR of run start date
            pathro:         output directory
            pathinit:       iSnobal init directory
            wy_start:       datetime of water year start date

        """
        # get logger
        self.logger = logger
        # get topo class
        self.topo = topo
        self.csys = cfg['grid']['csys']
        # get parameters from awsm
        self.init_file = cfg['files']['init_file']
        self.init_type = cfg['files']['init_type']

        if self.init_file is not None:
            self.logger.info('Using {} to build model init state.'.format(self.init_file))
        # iSnobal init directory
        self.pathinit = pathinit
        # type of model run
        self.model_type = cfg['awsm master']['model_type']
        # paths
        self.pathro = pathro
        # restart paramters
        self.restart_crash = cfg['isnobal restart']['restart_crash']
        self.restart_hr = cfg['isnobal restart']['wyh_restart_output']
        self.depth_thresh = cfg['isnobal restart']['depth_thresh']
        # water year hours
        self.start_wyhr = start_wyhr
        # datetime of october 1 of the correct year
        self.wy_start = wy_start

        # dictionary to store init data
        self.init = {}
        self.init['x'] = self.topo.x
        self.init['y'] = self.topo.y
        self.init['mask'] = self.topo.mask
        self.init['z_0'] = self.topo.roughness
        self.init['elevation'] = self.topo.dem
        # read in the init file
        self.get_init_file()

        for key in self.init.keys():
            self.init[key] = self.init[key].astype(np.float64)

        if self.model_type in ['ipysnobal', 'smrf_ipysnobal']:
            # convert temperatures to K
            self.init['T_s'] += FREEZE
            self.init['T_s_0'] += FREEZE
            if 'T_s_l' in self.init:
                self.init['T_s_l'] += FREEZE

        # write input file if running iSnobal and
        # not passed an iSnobal init file
        if (self.model_type == 'isnobal'):
            if self.init_type != 'ipw' or self.restart_crash is True:
                if self.restart_crash:
                    self.fp_init = os.path.join(pathinit,
                                                'init%04d.ipw' % (self.restart_hr))
                else:
                    self.fp_init = os.path.join(pathinit,
                                                'init%04d.ipw' % (self.start_wyhr))
                self.write_init()

    def get_init_file(self):
        """
        Get the necessary data from the init.
        This will check the model type and the init file and act accordingly.
        """
        # get crash restart if restart_crash
        if self.restart_crash:
            self.get_crash_init()
        # if we have no init info, make zero init
        elif self.init_file is None:
            self.get_zero_init()
        # get init depending on filetype
        elif self.init_type == 'ipw_out':
            self.get_ipw_out()
        elif self.init_type == 'netcdf':
            self.get_netcdf()
        elif self.init_type == 'netcdf_out':
            self.get_netcdf_out()
        # if none of these are true just leave the file type
        elif self.model_type == 'isnobal' and self.init_type == 'ipw':
            self.fp_init = self.init_file
        # if we're running some PySnobal and ipw init, get that
        elif self.init_type == 'ipw':
            self.get_ipw()

    def write_init(self):
        """
        Write the iSnobal init file
        """
        # get mask
        mask = self.topo.mask
        # make ipw init file
        i_out = ipw.IPW()
        i_out.new_band(self.init['elevation'])
        i_out.new_band(self.init['z_0'])
        i_out.new_band(self.init['z_s']*mask)  # snow depth
        i_out.new_band(self.init['rho']*mask)  # snow density

        i_out.new_band(self.init['T_s_0']*mask)  # active layer temp
        if self.start_wyhr > 0 or self.restart_crash:
            i_out.new_band(self.init['T_s_l']*mask)  # lower layer temp

        i_out.new_band(self.init['T_s']*mask)  # avgerage snow temp

        i_out.new_band(self.init['h2o_sat']*mask)  # percent saturatio
        i_out.add_geo_hdr([self.topo.u, self.topo.v],
                          [self.topo.du, self.topo.dv],
                          self.topo.units, self.csys)

        i_out.write(self.fp_init, 16)

    def get_crash_init(self):
        """
        Initializes simulation variables for special case when restarting a crashed
        run. Zeros depth under specified threshold and zeros other snow parameters
        that must be dealt with when depth is set to zero.

        Modifies:
            init:    dictionary of initialized variables
        """
        # restart procedure from failed run
        if self.model_type == 'isnobal':
            self.init_type = 'ipw_out'
            self.init_file = os.path.join(self.pathro, 'snow.%04d' % self.restart_hr)
            self.get_ipw_out()
        else:
            self.init_type = 'netcdf_out'
            self.init_file = os.path.join(self.pathro, 'snow.nc')
            self.get_netcdf_out()

        # zero depths under specified threshold
        restart_var = zero_crash_depths(self._logger,
                                        self.depth_thresh,
                                        self.init['z_s'],
                                        self.init['rho'],
                                        self.init['T_s_0'],
                                        self.init['T_s_l'],
                                        self.init['T_s'],
                                        self.init['h2o_sat'])
        # put variables back in init dictionary
        for k, v in restart_var.items():
            self.init[k] = v

    def get_zero_init(self):
        """
        Set init fields for zero init
        """
        self.logger.info('No init file given, using zero fields')
        self.init['z_s'] = 0.0*self.topo.mask  # snow depth
        self.init['rho'] = 0.0*self.topo.mask  # snow density

        self.init['T_s_0'] = -75.0*self.topo.mask  # active layer temp
        self.init['T_s_l'] = -75.0*self.topo.mask  # lower layer temp
        self.init['T_s'] = -75.0*self.topo.mask  # avgerage snow temp

        self.init['h2o_sat'] = 0.0*self.topo.mask  # percent saturatio

    def get_ipw_out(self):
        """
        Set init fields for iSnobal out as init file
        """
        i_in = ipw.IPW(self.init_file)
        self.init['z_s'] = i_in.bands[0].data*self.topo.mask  # snow depth
        self.init['rho'] = i_in.bands[1].data*self.topo.mask  # snow density

        self.init['T_s_0'] = i_in.bands[4].data*self.topo.mask  # active layer temp
        self.init['T_s_l'] = i_in.bands[5].data*self.topo.mask  # lower layer temp
        self.init['T_s'] = i_in.bands[6].data*self.topo.mask  # avgerage snow temp

        self.init['h2o_sat'] = i_in.bands[8].data*self.topo.mask  # percent saturation

    def get_ipw(self):
        """
        Set init fields for iSnobal out as init file
        """
        i_in = ipw.IPW(self.init_file)
        self.init['z_0'] = i_in.bands[1].data*self.topo.mask  # snow depth

        self.logger.warning('Using roughness from iSnobal ipw init file for initializing of model!')

        self.init['z_s'] = i_in.bands[2].data*self.topo.mask  # snow depth
        self.init['rho'] = i_in.bands[3].data*self.topo.mask  # snow density

        self.init['T_s_0'] = i_in.bands[4].data*self.topo.mask  # active layer temp

        # get bands depending on if there is a lower layer or not
        if len(i_in.bands) == 8:
            self.init['T_s_l'] = i_in.bands[5].data*self.topo.mask  # lower layer temp
            self.init['T_s'] = i_in.bands[6].data*self.topo.mask  # avgerage snow temp
            self.init['h2o_sat'] = i_in.bands[7].data*self.topo.mask  # percent saturation

        elif len(i_in.bands) == 7:
            self.init['T_s'] = i_in.bands[5].data*self.topo.mask  # avgerage snow temp
            self.init['h2o_sat'] = i_in.bands[6].data*self.topo.mask  # percent saturation


    def get_netcdf(self):
        """
        Get init fields from netcdf init file
        """
        i = nc.Dataset(self.init_file, 'r')

        # All other variables will be assumed zero if not present
        all_zeros = np.zeros_like(init['elevation'])
        flds = ['z_s', 'rho', 'T_s_0', 'T_s', 'h2o_sat', 'T_s_l']

        for f in flds:
            # if i.variables.has_key(f):
            if f in i.variables:
                self.init[f] = i.variables[f][:]         # read in the variables
            else:
                self.init[f] = all_zeros                 # default is set to zeros

        i.close()

    def get_netcdf_out(self):
        """
        Get init fields from output netcdf at correct time index
        """
        i = nc.Dataset(self.init_file)

        # find timestep indices to grab
        time = i.variables['time'][:]
        t_units = i.variables['time'].units
        nc_calendar = i.variables['time'].calendar
        nc_dates = nc.num2date(time, t_units, nc_calendar)
        # find offset of netcdf start
        offset = (nc_dates[0] - self.wy_start).total_seconds()//3600.0

        if self.restart_crash:
            tmpwyhr = self.restart_hr
        else:
            # start date water year hour
            tmpwyhr = self.start_wyhr

        # find closest location that the water year hours equal the restart hr
        idt = np.argmin(np.absolute(time - tmpwyhr))  # returns index
        if np.min(np.absolute(time - tmpwyhr)) > 24.0:
            # raise ValueError('No time in resatrt file that is within a day of restart time')
            self.logger.error('No time in resatrt file that is within a day of restart time')

        self.logger.warning('Initializing PySnobal with state from water year hour {}'.format(time[idt]))

        self.init['z_s'] = i.variables['thickness'][idt, :]
        self.init['rho'] = i.variables['snow_density'][idt, :]
        self.init['T_s_0'] = i.variables['temp_surf'][idt, :]
        self.init['T_s'] = i.variables['temp_snowcover'][idt, :]
        self.init['T_s_l'] = i.variables['temp_lower'][idt, :]
        self.init['h2o_sat'] = i.variables['water_saturation'][idt, :]

        i.close()

def zero_crash_depths(logger, depth_thresh, z_s, rho, T_s_0, T_s_l, T_s, h2o_sat):
    """
    Zero snow depth under certain threshold and deal with associated variables.

    Args:
        logger: logger instance
        depth_thresh: threshold in mm depth to zero
        z_s:    snow depth (Numpy array)
        rho:    snow density (Numpy array)
        T_s_0:  surface layer temperature (Numpy array)
        T_s_l:  lower layer temperature (Numpy array)
        T_s:    average snow cover temperature (Numpy array)
        h2o_sat: percent liquid h2o saturation (Numpy array)

    Returns:
        restart_var: dictionary of input variables after correction
    """

    # find pixels that need reset
    idz = z_s < depth_thresh

    # find number of pixels reset
    num_pix = len(np.where(idz)[0])
    num_pix_tot = z_s.size

    logger.warning('Zeroing depth in pixels lower than {} [m]'.format(depth_thresh))
    logger.warning('Zeroing depth in {} out of {} total pixels'.format(num_pix, num_pix_tot))

    z_s[idz] = 0.0
    rho[idz] = 0.0
    T_s_0[idz] = -75.0
    T_s_l[idz] = -75.0
    T_s[idz] = -75.0
    h2o_sat[idz] = 0.0

    restrat_var = {}
    restrat_var['z_s'] = z_s
    restrat_var['rho'] = rho
    restrat_var['T_s_0'] = T_s_0
    restrat_var['T_s_l'] = T_s_l
    restrat_var['T_s'] = T_s
    restrat_var['h2o_sat'] = h2o_sat

    return restrat_var
