import logging
import os
import sys
import coloredlogs
from datetime import datetime
import pandas as pd
import pytz
import copy
from inicheck.config import MasterConfig, UserConfig
from inicheck.tools import get_user_config, check_config
from inicheck.output import print_config_report, generate_config
from inicheck.tools import cast_all_variables
import smrf
# make input the same as raw input if python 2
try:
    input = raw_input
except NameError:
    pass

try:
    import snowav
except:
    print('snowav not installed')

from smrf import __core_config__ as __smrf_core_config__
from smrf import __recipes__ as __smrf_recipes__
from awsm import __core_config__ as __awsm_core_config__
from awsm import __config_titles__

from smrf.utils import utils, io
from awsm.convertFiles import convertFiles as cvf
from awsm.interface import interface as smin
from awsm.interface import smrf_ipysnobal as smrf_ipy
from awsm.interface import ingest_data
from awsm.utils import utilities as awsm_utils
from awsm.data.topo import topo as mytopo
from awsm.data.init_model import modelInit
import awsm.reporting.reportingtools as retools

class AWSM():
    """
    Args:
        configFile (str):  path to configuration file.

    Returns:
        AWSM class instance.

    Attributes:
    """

    def __init__(self, config):
        """
        Initialize the model, read config file, start and end date, and logging
        Args:
            config: string path to the config file or inicheck UserConfig instance
        """
        # read the config file and store
        awsm_mcfg = MasterConfig(modules = 'awsm')
        smrf_mcfg = MasterConfig(modules = 'smrf')

        if isinstance(config, str):
            if not os.path.isfile(config):
                raise Exception('Configuration file does not exist --> {}'
                                .format(config))
            configFile = config

            try:
                try:
                    snowav_mcfg = MasterConfig(modules = 'snowav')
                    combined_mcfg = MasterConfig(modules = ['smrf','awsm','snowav'])
                except:
                    combined_mcfg = MasterConfig(modules = ['smrf','awsm'])

                # Read in the original users config
                self.ucfg = get_user_config(configFile, mcfg=combined_mcfg)
                self.configFile = configFile

            except UnicodeDecodeError as e:
                print(e)
                raise Exception(('The configuration file is not encoded in '
                                 'UTF-8, please change and retry'))

        elif isinstance(config, UserConfig):
            self.ucfg = config
            configFile = ''

        else:
            raise Exception('Config passed to AWSM is neither file name nor UserConfig instance')

        # get the git version
        self.gitVersion = awsm_utils.getgitinfo()

        # create blank log and error log because logger is not initialized yet
        self.tmp_log = []
        self.tmp_err = []
        self.tmp_warn = []

        # Check the user config file for errors and report issues if any
        self.tmp_log.append("Checking config file for issues...")
        warnings, errors = check_config(self.ucfg)
        print_config_report(warnings, errors)

        self.config = self.ucfg.cfg

        # Exit AWSM if config file has errors
        if len(errors) > 0:
            print("Errors in the config file. "
                  "See configuration status report above.")
            sys.exit()

        # ################## Decide which modules to run #####################
        self.do_smrf = self.config['awsm master']['run_smrf']
        #self.do_isnobal = self.config['awsm master']['run_isnobal']
        self.model_type = self.config['awsm master']['model_type']
        # self.do_smrf_ipysnobal = \
        #     self.config['awsm master']['run_smrf_ipysnobal']
        # self.do_ipysnobal = self.config['awsm master']['run_ipysnobal']

        if 'gridded' in self.config:
            self.do_forecast = self.config['gridded']['forecast_flag']
            self.n_forecast_hours = self.config['gridded']['n_forecast_hours']
        else:
            self.do_forecast = False

        # options for converting files
        self.do_make_in = self.config['awsm master']['make_in']
        self.do_make_nc = self.config['awsm master']['make_nc']
        # do report?
        self.do_report = self.config['awsm master']['run_report']

        # options for masking isnobal
        self.mask_isnobal = self.config['awsm master']['mask_isnobal']

        # prompt for making directories
        self.prompt_dirs = self.config['awsm master']['prompt_dirs']

        # store smrf version if running smrf
        self.smrf_version = utils.getgitinfo()

        # ################ Time information ##################
        self.start_date = pd.to_datetime(self.config['time']['start_date'])
        self.end_date = pd.to_datetime(self.config['time']['end_date'])
        self.time_step = self.config['time']['time_step']
        self.tmz = self.config['time']['time_zone']
        self.tzinfo = pytz.timezone(self.config['time']['time_zone'])
        # date to use for finding wy
        tmp_date = self.start_date.replace(tzinfo=self.tzinfo)
        tmp_end_date = self.end_date.replace(tzinfo=self.tzinfo)

        # find water year hour of start and end date
        self.start_wyhr = int(utils.water_day(tmp_date)[0]*24)
        self.end_wyhr = int(utils.water_day(tmp_end_date)[0]*24)

        # find start of water year
        tmpwy = utils.water_day(tmp_date)[1] - 1
        self.wy_start = pd.to_datetime('{:d}-10-01'.format(tmpwy))

        # ################ Store some paths from config file ##################
        # path to the base drive (i.e. /data/blizzard)
        if self.config['paths']['path_dr'] is not None:
            self.path_dr = os.path.abspath(self.config['paths']['path_dr'])
        else:
            print('No base path to drive given. Exiting now!')
            sys.exit()

        # name of your basin (i.e. Tuolumne)
        self.basin = self.config['paths']['basin']
        # water year of run
        self.wy = utils.water_day(tmp_date)[1]
        # if the run is operational or not
        self.isops = self.config['paths']['isops']
        # name of project if not an operational run
        self.proj = self.config['paths']['proj']
        # check for project description
        self.desc = self.config['paths']['desc']
        # find style for folder date stamp
        self.folder_date_style = self.config['paths']['folder_date_style']

        # setting to output in seperate daily folders
        self.daily_folders = self.config['awsm system']['daily_folders']
        if self.daily_folders and not self.run_smrf_ipysnobal:
            raise ValueError('Cannot run daily_folders with anything other'
                             ' than run_smrf_ipysnobal')

        if self.do_forecast:
            self.tmp_log.append('Forecasting set to True')

            # self.fp_forecastdata = self.config['gridded']['file']
            # if self.fp_forecastdata is None:
            #     self.tmp_err.append('Forecast set to true, '
            #                         'but no grid file given')
            #     print("Errors in the config file. See configuration "
            #           "status report above.")
            #     print(self.tmp_err)
            #     sys.exit()

            if self.config['system']['threading']:
                # Can't run threaded smrf if running forecast_data
                self.tmp_err.append('Cannot run SMRF threaded with'
                                    ' gridded input data')
                print(self.tmp_err)
                sys.exit()

        # Time step mass thresholds for iSnobal
        self.mass_thresh = []
        self.mass_thresh.append(self.config['grid']['thresh_normal'])
        self.mass_thresh.append(self.config['grid']['thresh_medium'])
        self.mass_thresh.append(self.config['grid']['thresh_small'])

        # threads for running iSnobal
        self.ithreads = self.config['awsm system']['ithreads']
        # how often to output form iSnobal
        self.output_freq = self.config['awsm system']['output_frequency']
        # number of timesteps to run if ou don't want to run the whole thing
        self.run_for_nsteps = self.config['awsm system']['run_for_nsteps']
        # pysnobal output variables
        self.pysnobal_output_vars = self.config['awsm system']['variables']
        self.pysnobal_output_vars = [wrd.lower() for wrd in self.pysnobal_output_vars]
        # snow and emname
        self.snow_name = self.config['awsm system']['snow_name']
        self.em_name = self.config['awsm system']['em_name']

        # options for restarting iSnobal
        self.restart_crash = False
        if self.config['isnobal restart']['restart_crash']:
            self.restart_crash = True
            # self.new_init = self.config['isnobal restart']['new_init']
            self.depth_thresh = self.config['isnobal restart']['depth_thresh']
            self.restart_hr = \
                int(self.config['isnobal restart']['wyh_restart_output'])

        # iSnobal active layer
        self.active_layer = self.config['grid']['active_layer']

        # if we are going to run ipysnobal with smrf
        if self.model_type in ['ipysnobal', 'smrf_ipysnobal']:
            self.ipy_threads = self.ithreads
            self.ipy_init_type = \
                self.config['files']['init_type']
            self.forcing_data_type = \
                self.config['ipysnobal']['forcing_data_type']

        # parameters needed for restart procedure
        self.restart_run = False
        if self.config['isnobal restart']['restart_crash']:
            self.restart_run = True
            # find restart hour datetime
            reset_offset = pd.to_timedelta(self.restart_hr, unit='h')
            # set a new start date for this run
            self.restart_date = self.start_date + reset_offset
            self.tmp_log.append('Restart date is {}'.format(self.start_date))

        # read in update depth parameters
        self.update_depth = False
        if 'update depth' in self.config:
            self.update_depth = self.config['update depth']['update']
        if self.update_depth:
            self.update_file = self.config['update depth']['update_file']
            self.update_buffer = self.config['update depth']['buffer']
            self.flight_numbers = self.config['update depth']['flight_numbers']
            # if flights to use is not list, make it a list
            if self.flight_numbers is not None:
                if not isinstance(self.flight_numbers, list):
                    self.flight_numbers = [self.flight_numbers]

        # list of sections releated to AWSM
        # These will be removed for smrf config
        self.sec_awsm = awsm_mcfg.cfg.keys()
        self.sec_smrf = smrf_mcfg.cfg.keys()

        # Make rigid directory structure
        self.mk_directories()

        # ################ Topo information ##################
        self.topotype = self.config['topo']['type']

        # ################ Topo data for iSnobal ##################
        # get topo stats
        self.csys = self.config['grid']['csys']
        self.nbits = int(self.config['grid']['nbits'])
        self.soil_temp = self.config['soil_temp']['temp']
        # get topo class
        self.topo = mytopo(self.config['topo'], self.mask_isnobal,
                           self.model_type, self.csys, self.pathdd)

        # parse reporting section and make reporting folder
        if self.do_report:
            self.parseReport()

        # ################ Generate config backup ##################
        if self.config['output']['input_backup']:

            # set location for backup and output backup of awsm sections
            config_backup_location = \
                os.path.join(self.pathdd, 'awsm_config_backup.ini')
            generate_config(self.ucfg, config_backup_location)

        # create log now that directory structure is done
        self.createLog()

        # if we have a model, initialize it
        if self.model_type is not None:
            self.myinit = modelInit(self._logger, self.config, self.topo,
                                    self.start_wyhr, self.pathro,
                                    self.pathinit, self.wy_start)


    def parseReport(self):
        """
        Parse the options related to reporting
        """
        # get all relevant options
        snowav_mcfg = MasterConfig(modules = 'snowav')
        self.sec_snowav = snowav_mcfg.cfg.keys()
        # make reporting directory
        self.path_report_o = os.path.join(self.path_wy, 'reports')
        self.path_report_i = os.path.join(self.path_report_o, 'report_{}'.format(self.folder_date_stamp))
        if not os.path.exists(self.path_report_i):
            os.makedirs(self.path_report_i)

        # fill in some of the config options
        self.config['report']['rep_path'] = self.path_report_i
        self.config['snowav system']['save_path'] = self.path_report_i
        self.config['snowav system']['wy'] = self.wy
        self.config['runs']['run_dirs'] = [self.pathro]
        # create updated config for report
        self.report_config = os.path.join(self.path_report_o, 'snowav_cfg.ini')
        #generate_config(self.ucfg, self.report_config)

        ##### new stuff
        # Write out config file to run smrf
        # make copy and delete only awsm sections
        snowav_cfg = copy.deepcopy(self.ucfg)
        for key in self.ucfg.cfg.keys():
            if key in self.sec_awsm or key in self.sec_smrf:
                del snowav_cfg.cfg[key]

        self.tmp_log.append('Writing the config file for snowav')
        generate_config(snowav_cfg, self.report_config)

    def createLog(self):
        '''
        Now that the directory structure is done, create log file and print out
        saved logging statements.
        '''

        level_styles = {'info': {'color': 'white'},
                        'notice': {'color': 'magenta'},
                        'verbose': {'color': 'blue'},
                        'success': {'color': 'green', 'bold': True},
                        'spam': {'color': 'green', 'faint': True},
                        'critical': {'color': 'red', 'bold': True},
                        'error': {'color': 'red'},
                        'debug': {'color': 'green'},
                        'warning': {'color': 'yellow'}}

        field_styles =  {'hostname': {'color': 'magenta'},
                         'programname': {'color': 'cyan'},
                         'name': {'color': 'white'},
                         'levelname': {'color': 'white', 'bold': True},
                         'asctime': {'color': 'green'}}

        # start logging
        loglevel = self.config['awsm system']['log_level'].upper()

        numeric_level = getattr(logging, loglevel, None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % loglevel)

        # setup the logging
        logfile = None
        if self.config['awsm system']['log_to_file']:
            if self.config['isnobal restart']['restart_crash']:
                logfile = \
                    os.path.join(self.pathll,
                                 'log_restart_{}.out'.format(self.restart_hr))
            elif self.do_forecast:
                logfile = \
                    os.path.join(self.pathll,
                                 'log_forecast_'
                                 '{}.out'.format(self.folder_date_stamp))
            else:
                logfile = \
                    os.path.join(self.pathll,
                                 'log_{}.out'.format(self.folder_date_stamp))
            # let user know
            print('Logging to file: {}'.format(logfile))

        fmt = '%(levelname)s:%(name)s:%(message)s'
        if logfile is not None:
            logging.basicConfig(filename=logfile,
                                filemode='w',
                                level=numeric_level,
                                format=fmt)

            # section of code needed to make new log file on daily runs
            fileh = logging.FileHandler(logfile, 'a')
            #formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            #fileh.setFormatter(formatter)

            log = logging.getLogger()  # root logger
            for hdlr in log.handlers[:]:  # remove all old handlers
                log.removeHandler(hdlr)
            log.addHandler(fileh)      #

        else:
            logging.basicConfig(level=numeric_level)
            coloredlogs.install(level=numeric_level,
                                fmt=fmt,
                                level_styles=level_styles,
                                field_styles=field_styles)

        self._loglevel = numeric_level

        self._logger = logging.getLogger(__name__)

        # print title and mountains
        title, mountain = self.title()
        for line in mountain:
            self._logger.info(line)
        for line in title:
            self._logger.info(line)
        # dump saved logs
        if len(self.tmp_log) > 0:
            for l in self.tmp_log:
                self._logger.info(l)
        if len(self.tmp_warn) > 0:
            for l in self.tmp_warn:
                self._logger.warning(l)
        if len(self.tmp_err) > 0:
            for l in self.tmp_err:
                self._logger.error(l)

    def runSmrf(self):
        """
        Run smrf. Calls :mod: `awsm.interface.interface.smrfMEAS`
        """
        # modify config and run smrf
        smin.smrfMEAS(self)

    def nc2ipw(self, runtype):
        """
        Convert ipw smrf output to isnobal inputs
        """
        cvf.nc2ipw_mea(self, runtype)

    def ipw2nc(self, runtype):
        """
        Convert ipw output to netcdf files. Calls
        :mod: `awsm.convertFiles.convertFiles.ipw2nc_mea`
        """
        cvf.ipw2nc_mea(self, runtype)

    def run_isnobal(self, offset=None):
        """
        Run isnobal. Calls :mod: `awsm.interface.interface.run_isnobal`
        """

        smin.run_isnobal(self, offset=offset)

    def run_isnobal_update(self):
        """
        Run iSnobal with update procedure
        """
        # initialize updater
        updater = ingest_data.StateUpdater(self)
        # run iSnobal with updates
        updater.run_update_procedure_isnobal(self)

    def run_smrf_ipysnobal(self):
        """
        Run smrf and pass inputs to ipysnobal in memory.
        Calls :mod: `awsm.interface.smrf_ipysnobal.run_smrf_ipysnobal`
        """

        smrf_ipy.run_smrf_ipysnobal(self)

    def run_awsm_daily(self):
        """
        This function runs :mod: `awsm.interface.smrf_ipysnobal.run_smrf_ipysnobal`
        on an hourly output from Pysnobal, outputting to daily folders, similar
        to the HRRR froecast.
        """

        smin.run_awsm_daily(self)

    def run_ipysnobal(self):
        """
        Run PySnobal from previously run smrf forcing data
        Calls :mod: `awsm.interface.smrf_ipysnobal.run_ipysnobal`
        """
        smrf_ipy.run_ipysnobal(self)

    def mk_directories(self):
        """
        Create all needed directories starting from the working drive
        """
        # rigid directory work
        self.tmp_log.append('AWSM creating directories')

        # string to append to folders indicatiing run start and end
        if self.folder_date_style == 'wyhr':
            self.folder_date_stamp = '{:04d}_{:04d}'.format(self.start_wyhr,
                                                            self.end_wyhr)

        elif self.folder_date_style == 'day':
            self.folder_date_stamp = \
                '{}'.format(self.start_date.strftime("%Y%m%d"))

        elif self.folder_date_style == 'start_end':
            self.folder_date_stamp = \
                '{}_{}'.format(self.start_date.strftime("%Y%m%d"),
                               self.end_date.strftime("%Y%m%d"))

        # make basin path
        self.path_ba = os.path.join(self.path_dr, self.basin)

        # check if ops or dev
        if self.isops:
            opsdev = 'ops'
        else:
            opsdev = 'devel'
        # assign paths accordinly
        self.path_od = os.path.join(self.path_ba, opsdev)
        self.path_wy = os.path.join(self.path_od, 'wy{}'.format(self.wy))
        self.path_wy = os.path.join(self.path_wy, self.proj)

        # specific data folder conatining
        self.pathd = os.path.join(self.path_wy, 'data')
        self.pathr = os.path.join(self.path_wy, 'runs')
        # log folders
        self.pathlog = os.path.join(self.path_wy, 'logs')
        self.pathll = os.path.join(self.pathlog,
                                   'log{}'.format(self.folder_date_stamp))

        # name of temporary smrf file to write out
        self.smrfini = os.path.join(self.path_wy, 'tmp_smrf_config.ini')
        self.forecastini = os.path.join(self.path_wy,
                                        'tmp_smrf_forecast_config.ini')

        if not self.do_forecast:
            # assign path names for isnobal, path_names_att will be used
            # to create necessary directories
            path_names_att = ['pathdd', 'pathrr', 'pathi',
                              'pathinit', 'pathro', 'paths', 'path_ppt']
            self.pathdd = \
                os.path.join(self.pathd,
                             'data{}'.format(self.folder_date_stamp))
            self.pathrr = \
                os.path.join(self.pathr,
                             'run{}'.format(self.folder_date_stamp))
            self.pathi = os.path.join(self.pathdd, 'input/')
            self.pathinit = os.path.join(self.pathdd, 'init/')
            self.pathro = os.path.join(self.pathrr, 'output/')
            self.paths = os.path.join(self.pathdd, 'smrfOutputs')
            self.ppt_desc = \
                os.path.join(self.pathdd,
                             'ppt_desc{}.txt'.format(self.folder_date_stamp))
            self.path_ppt = os.path.join(self.pathdd, 'ppt_4b')

            # used to check if data direcotry exists
            check_if_data = self.pathdd
        else:
            path_names_att = ['pathdd', 'pathrr', 'pathi',
                              'pathinit', 'pathro', 'paths', 'path_ppt']
            self.pathdd = \
                os.path.join(self.pathd,
                             'forecast{}'.format(self.folder_date_stamp))
            self.pathrr = \
                os.path.join(self.pathr,
                             'forecast{}'.format(self.folder_date_stamp))
            self.pathi = os.path.join(self.pathdd, 'input/')
            self.pathinit = os.path.join(self.pathdd, 'init/')
            self.pathro = os.path.join(self.pathrr, 'output/')
            self.paths = os.path.join(self.pathdd, 'smrfOutputs')
            self.ppt_desc = \
                os.path.join(self.pathdd,
                             'ppt_desc{}.txt'.format(self.folder_date_stamp))
            self.path_ppt = os.path.join(self.pathdd, 'ppt_4b')

            # used to check if data direcotry exists
            check_if_data = self.pathdd

        # add log path to create directory
        path_names_att.append('pathll')

        # Only start if your drive exists
        if os.path.exists(self.path_dr):
            # If the specific path to your WY does not exist,
            # create it and following directories/
            # If the working path specified in the config file does not exist
            if not os.path.exists(self.path_wy):
                y_n = 'a'  # set a funny value to y_n
                # while it is not y or n (for yes or no)
                while y_n not in ['y', 'n']:
                    if self.prompt_dirs:
                        y_n = input('Directory %s does not exist. Create base '
                                    'directory and all subdirectories? '
                                    '(y n): ' % self.path_wy)
                    else:
                        y_n = 'y'

                if y_n == 'n':
                    self.tmp_err.append('Please fix the base directory'
                                        ' (path_wy) in your config file.')
                    print(self.tmp_err)
                    sys.exit()
                elif y_n == 'y':
                    self.make_rigid_directories(path_names_att)

            # If WY exists, but not this exact run for the dates, create it
            elif not os.path.exists(check_if_data):
                y_n = 'a'
                while y_n not in ['y', 'n']:
                    if self.prompt_dirs:
                        y_n = input('Directory %s does not exist. Create base '
                                    'directory and all subdirectories? '
                                    '(y n): ' % check_if_data)
                    else:
                        y_n = 'y'

                if y_n == 'n':
                    self.tmp_err.append('Please fix the base directory'
                                        ' (path_wy) in your config file.')
                    print(self.tmp_err)
                    sys.exit()
                elif y_n == 'y':
                    self.make_rigid_directories(path_names_att)

            else:
                self.tmp_warn.append('Directory structure leading to '
                                     '{} already exists.'.format(check_if_data))

            # make sure runs exists
            if not os.path.exists(os.path.join(self.path_wy, 'runs/')):
                os.makedirs(os.path.join(self.path_wy, 'runs/'))

            # if we're not running forecast, make sure path to outputs exists
            if not os.path.exists(self.pathro):
                os.makedirs(self.pathro)

            # find where to write file
            fp_desc = os.path.join(self.path_wy, 'projectDescription.txt')

            if not os.path.isfile(fp_desc):
                # look for description or prompt for one
                if self.desc is not None:
                    pass
                else:
                    self.desc = input('\nNo description for project. '
                                      'Enter one now, but do not use '
                                      'any punctuation:\n')
                f = open(fp_desc, 'w')
                f.write(self.desc)
                f.close()
            else:
                self.tmp_log.append('Description file aleardy exists\n')

        else:
            self.tmp_err.append('Base directory did not exist, '
                                'not safe to conitnue. Make sure base '
                                'directory exists before running.')
            print(self.tmp_err)
            sys.exit()

    def make_rigid_directories(self, path_name):
        """
        Creates rigid directory structure from list of relative bases and
        extensions from the base
        """
        # loop through lists
        for idp, pn in enumerate(path_name):
            # get attribute of path
            path = getattr(self, pn)

            if not os.path.exists(path):
                os.makedirs(path)
            else:
                self.tmp_log.append('Directory --{}-- exists, not creating.\n')

    def do_reporting(self):
        """
        Outer most function for controlling what gets plotted to the dashboard
        or written to the report
        """
        retools.plot_dashboard(self)

    def title(self):
        """
        AWSM titles
        Text generated from:    http://patorjk.com/software/taag/#p=testall&f=Swamp%20Land&t=AWSM
        Mountain ascii from:    https://www.ascii-code.com/ascii-art/nature/mountains.php
        """
        mountain = ["                      _  ",
                    "                     /#\    ",
                    "                    /###\     /\    ",
                    "                   /  ###\   /##\  /\   ",
                    "                  /      #\ /####\/##\  ",
                    "                 /  /      /   # /  ##\             _       /\  ",
                    "               // //  /\  /    _/  /  #\ _         /#\    _/##\    /\   ",
                    "              // /   /  \     /   /    #\ \      _/###\_ /   ##\__/ _\ ",
                    "             /  \   / .. \   / /   _   { \ \   _/       / //    /    \\    ",
                    "     /\     /    /\  ...  \_/   / / \   } \ | /  /\  \ /  _    /  /    \ /\    ",
                    "  _ /  \  /// / .\  ..%:.  /... /\ . \ {:  \\   /. \     / \  /   ___   /  \   ",
                    " /.\ .\.\// \/... \.::::..... _/..\ ..\:|:. .  / .. \\  /.. \    /...\ /  \ \  ",
                    "/...\.../..:.\. ..:::::::..:..... . ...\{:... / %... \\/..%. \  /./:..\__   \  ",
                    " .:..\:..:::....:::;;;;;;::::::::.:::::.\}.....::%.:. \ .:::. \/.%:::.:..\ ",
                    "::::...:::;;:::::;;;;;;;;;;;;;;:::::;;::{:::::::;;;:..  .:;:... ::;;::::.. ",
                    ";;;;:::;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;];;;;;;;;;;::::::;;;;:.::;;;;;;;;:..  ",
                    ";;;;;;;;;;;;;;ii;;;;;;;;;;;;;;;;;;;;;;;;[;;;;;;;;;;;;;;;;;;;;;;:;;;;;;;;;;;;;  ",
                    ";;;;;;;;;;;;;;;;;;;iiiiiiii;;;;;;;;;;;;;;};;ii;;iiii;;;;i;;;;;;;;;;;;;;;ii;;;  ",
                    "iiii;;;iiiiiiiiiiIIIIIIIIIIIiiiiiIiiiiii{iiIIiiiiiiiiiiiiiiii;;;;;iiiilliiiii  ",
                    "IIIiiIIllllllIIlllIIIIlllIIIlIiiIIIIIIIIIIIIlIIIIIllIIIIIIIIiiiiiiiillIIIllII  ",
                    "IIIiiilIIIIIIIllTIIIIllIIlIlIIITTTTlIlIlIIIlIITTTTTTTIIIIlIIllIlIlllIIIIIIITT  ",
                    "IIIIilIIIIITTTTTTTIIIIIIIIIIIIITTTTTIIIIIIIIITTTTTTTTTTIIIIIIIIIlIIIIIIIITTTT  ",
                    "IIIIIIIIITTTTTTTTTTTTTIIIIIIIITTTTTTTTIIIIIITTTTTTTTTTTTTTIIIIIIIIIIIIIITTTTT  ",
                    "",
                    "",
                    ""]
        title = [
                '               AAA   WWWWWWWW                           WWWWWWWW   SSSSSSSSSSSSSSS MMMMMMMM               MMMMMMMM',
                '              A:::A  W::::::W                           W::::::W SS:::::::::::::::SM:::::::M             M:::::::M',
                '             A:::::A W::::::W                           W::::::WS:::::SSSSSS::::::SM::::::::M           M::::::::M',
                '            A:::::::AW::::::W                           W::::::WS:::::S     SSSSSSSM:::::::::M         M:::::::::M',
                '           A:::::::::AW:::::W           WWWWW           W:::::W S:::::S            M::::::::::M       M::::::::::M',
                '          A:::::A:::::AW:::::W         W:::::W         W:::::W  S:::::S            M:::::::::::M     M:::::::::::M',
                '         A:::::A A:::::AW:::::W       W:::::::W       W:::::W    S::::SSSS         M:::::::M::::M   M::::M:::::::M',
                '        A:::::A   A:::::AW:::::W     W:::::::::W     W:::::W      SS::::::SSSSS    M::::::M M::::M M::::M M::::::M',
                '       A:::::A     A:::::AW:::::W   W:::::W:::::W   W:::::W         SSS::::::::SS  M::::::M  M::::M::::M  M::::::M',
                '      A:::::AAAAAAAAA:::::AW:::::W W:::::W W:::::W W:::::W             SSSSSS::::S M::::::M   M:::::::M   M::::::M',
                '     A:::::::::::::::::::::AW:::::W:::::W   W:::::W:::::W                   S:::::SM::::::M    M:::::M    M::::::M',
                '    A:::::AAAAAAAAAAAAA:::::AW:::::::::W     W:::::::::W                    S:::::SM::::::M     MMMMM     M::::::M',
                '   A:::::A             A:::::AW:::::::W       W:::::::W         SSSSSSS     S:::::SM::::::M               M::::::M',
                '  A:::::A               A:::::AW:::::W         W:::::W          S::::::SSSSSS:::::SM::::::M               M::::::M',
                ' A:::::A                 A:::::AW:::W           W:::W           S:::::::::::::::SS M::::::M               M::::::M',
                'AAAAAAA                   AAAAAAAWWW             WWW             SSSSSSSSSSSSSSS   MMMMMMMM               MMMMMMMM'
                ]

        return title, mountain

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Provide some logging info about when AWSM was closed
        """

        self._logger.info('AWSM closed --> %s' % datetime.now())


def run_awsm_daily_ops(config_file):
    """
    Run each day seperately. Calls run_awsm
    """
    # define some formats
    fmt_day = '%Y%m%d'
    fmt_cfg = '%Y-%m-%d %H:%M'
    add_day = pd.to_timedelta(24, unit='h')

    # get config instance
    config = get_user_config(config_file,
                             modules = ['smrf','awsm', 'snowav'])

    # copy the config and get total start and end
    # config = deepcopy(base_config)
    # set naming style
    config.raw_cfg['paths']['folder_date_style'] = 'day'

    config.apply_recipes()
    config = cast_all_variables(config, config.mcfg)
    model_start = config.cfg['time']['start_date']
    model_end = config.cfg['time']['end_date']
    isops = config.cfg['paths']['isops']
    if isops:
        devops = 'ops'
    else:
        devops = 'devel'

    # find output location for previous output
    paths = config.cfg['paths']
    tzinfo = pytz.timezone(config.cfg['time']['time_zone'])
    wy = utils.water_day(model_start.replace(tzinfo=tzinfo))[1]
    prev_out_base = os.path.join(paths['path_dr'],
                                 paths['basin'],
                                 devops,
                                 'wy{}'.format(wy),
                                 paths['proj'],
                                 'runs')

    # find day of start and end
    start_day = pd.to_datetime(model_start.strftime(fmt_day))
    end_day = pd.to_datetime(model_end.strftime(fmt_day))

    # find total range of run
    ndays = int((end_day-start_day).days) + 1
    date_list = [start_day + pd.to_timedelta(x, unit='D') for x in range(0, ndays)]

    # loop through daily runs and run awsm
    for idd, sd in enumerate(date_list):
        new_config = copy.deepcopy(config)
        # get the end of the day
        ed = sd + add_day

        # make sure we're in the model date range
        if sd < model_start:
            sd = model_start
        if ed > model_end:
            ed = model_end

        # set the start and end dates
        new_config.raw_cfg['time']['start_date'] = sd.strftime(fmt_cfg)
        new_config.raw_cfg['time']['end_date'] = ed.strftime(fmt_cfg)

        # reset the initialization
        if idd > 0:
            # find previous output file
            prev_day = sd - pd.to_timedelta(1, unit='D')
            prev_out = os.path.join(prev_out_base,
                                    'run{}'.format(prev_day.strftime(fmt_day)),
                                    'snow.nc')
            # reset if running the model
            if new_config.cfg['awsm master']['model_type'] is not None:
                new_config.raw_cfg['files']['init_type'] = 'netcdf_out'
                new_config.raw_cfg['files']['init_file'] = prev_out

        # apply recipes with new setttings
        new_config.apply_recipes()
        new_config = cast_all_variables(new_config, new_config.mcfg)

        # run awsm for the day
        run_awsm(new_config)


def run_awsm(config):
    """
    Function that runs awsm how it should be operate for full runs.

    Args:
        config: string path to the config file or inicheck UserConfig instance
    """
    start = datetime.now()

    with AWSM(config) as a:
        if a.do_forecast:
            runtype = 'forecast'
        else:
            runtype = 'smrf'

        if not a.config['isnobal restart']['restart_crash']:
            # distribute data by running smrf
            if a.do_smrf:
                a.runSmrf()

            # convert smrf output to ipw for iSnobal
            if a.do_make_in:
                a.nc2ipw(runtype)

            if a.model_type == 'isnobal':
                # run iSnobal
                if a.update_depth:
                    a.run_isnobal_update()
                else:
                    a.run_isnobal()

            elif a.model_type == 'ipysnobal':
                # run iPySnobal
                a.run_ipysnobal()

                # convert ipw back to netcdf for processing
            if a.do_make_nc:
                a.ipw2nc(runtype)
        # if restart
        else:
            if a.model_type == 'isnobal':
                # restart iSnobal from crash
                if a.update_depth:
                    a.run_isnobal_update()
                else:
                    a.run_isnobal()
                # convert ipw back to netcdf for processing
            elif a.model_type == 'ipysnobal':
                # run iPySnobal
                a.run_ipysnobal()

            if a.do_make_nc:
                a.ipw2nc(runtype)

        # Run iPySnobal from SMRF in memory
        if a.model_type == 'smrf_ipysnobal':
            if a.daily_folders:
                a.run_awsm_daily()
            else:
                a.run_smrf_ipysnobal()

        # create report
        if a.do_report:
            a._logger.info('AWSM finished run, starting report')
            a.do_reporting()

            a._logger.info('AWSM finished in: {}'.format(datetime.now() - start))



def can_i_run_awsm(config):
    """
    Function that wraps run_awsm in try, except.

    Args:
        config: string path to the config file or inicheck UserConfig instance
    """
    try:
        run_awsm(config)
        success = True
        return success

    except Exception as e:
        print(e)
        return False
