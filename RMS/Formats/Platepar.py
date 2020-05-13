""" CMN-style astrometric calibration.
"""

# The MIT License

# Copyright (c) 2016 Denis Vida

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import print_function, division, absolute_import

import os
import json
import copy
import datetime


import numpy as np
import scipy.optimize

from RMS.Astrometry.Conversions import date2JD, jd2Date
import RMS.Astrometry.ApplyAstrometry
from RMS.Math import angularSeparation



class stationData(object):
    """ Holds information about one meteor station (location) and observed points.
    """

    def __init__(self, file_name):
        self.file_name = file_name
        self.station_code = ''
        self.lon = 0
        self.lat = 0
        self.h = 0
        self.points = []

    def __str__(self):
        return 'Station: ' + self.station_code + ' data points: ' + str(len(self.points))


def parseInf(file_name):
    """ Parse information from an INF file to a stationData object.
    """

    station_data_obj = stationData(file_name)

    with open(file_name) as f:
        for line in f.readlines()[2:]:
            
            line = line.split()

            if 'Station_Code' in line[0]:
                station_data_obj.station_code = line[1]

            elif 'Long' in line[0]:
                station_data_obj.lon = float(line[1])

            elif 'Lati' in line[0]:
                station_data_obj.lat = float(line[1])

            elif 'Height' in line[0]:
                station_data_obj.h = int(line[1])

            else:
                station_data_obj.points.append(map(float, line))


    return station_data_obj



def getCatalogStarsImagePositions(catalog_stars, jd, platepar):
    """ Get image positions of catalog stars using the current platepar values. 

    Arguments:
        catalog_stars: [2D list] A list of (ra, dec, mag) pairs of catalog stars.
        jd: [float] Julian date for transformation.
        platepar: [Platepar]

    Return:
        (x_array, y_array mag_catalog): [tuple of ndarrays] X, Y positons and magnitudes of stars on the 
            image.
    """

    ra_catalog, dec_catalog, mag_catalog = catalog_stars.T

    # Convert star RA, Dec to image coordinates
    x_array, y_array = RMS.Astrometry.ApplyAstrometry.raDecToXYPP(ra_catalog, dec_catalog, jd, platepar)

    return x_array, y_array, mag_catalog



def getPairedStarsSkyPositions(img_x, img_y, jd, platepar):
    """ Compute RA, Dec of all paired stars on the image given the platepar. 

    Arguments:
        img_x: [ndarray] Array of column values of the stars.
        img_y: [ndarray] Array of row values of the stars.
        jd: [float] Julian date for transformation.
        platepar: [Platepar instance] Platepar object.

    Return:
        (ra_array, dec_array): [tuple of ndarrays] Arrays of RA and Dec of stars on the image.
    """

    # Compute RA, Dec of image stars
    img_time = jd2Date(jd)
    _, ra_array, dec_array, _ = RMS.Astrometry.ApplyAstrometry.xyToRaDecPP(len(img_x)*[img_time], img_x, 
        img_y, len(img_x)*[1], platepar)

    return ra_array, dec_array


class Platepar(object):
    def __init__(self, distortion_type="poly3+radial"):
        """ Astrometric and photometric calibration plate parameters. Several distortion types are supported.
    
        Arguments:
            file_name: [string] Path to the platepar file.

        Keyword arguments:
            distortion_type: [str] Distortion type. It can be one of the following:
                - "poly3+radial" - 3rd order polynomial fit including a single radial term
                - "radial3" - 3rd order radial distortion
                - "radial5" - 5th order radial distortion
    
        Return:
            self: [object] Instance of this class with loaded platepar parameters.

        """

        self.version = 2

        # Set the distortion type
        self.distortion_type = distortion_type
        self.setDistortionType(self.distortion_type)



        # Station coordinates
        self.lat = self.lon = self.elev = 0

        # Reference time and date
        self.time = 0
        self.JD = 0

        # UT correction
        self.UT_corr = 0

        self.Ho = 0
        self.X_res = 0
        self.Y_res = 0
        self.focal_length = 0

        self.fov_h = 0
        self.fov_v = 0

        # FOV centre
        self.RA_d = self.RA_H = self.RA_M = self.RA_S = 0
        self.dec_d = self.dec_D = self.dec_M = self.dec_S = 0
        self.pos_angle_ref = 0
        self.rotation_from_horiz = 0

        self.az_centre = 0
        self.alt_centre = 0

        # FOV scale (px/deg)
        self.F_scale = 1.0

        # Photometry calibration
        self.mag_0 = -2.5
        self.mag_lev = 1.0
        self.mag_lev_stddev = 0.0
        self.gamma = 1.0
        self.vignetting_coeff = 0.0

        self.station_code = None

        self.star_list = None

        # Flag to indicate that the platepar was refined with CheckFit
        self.auto_check_fit_refined = False

        # Flag to indicate that the platepar was successfuly auto recalibrated on an individual FF files
        self.auto_recalibrated = False

        # Init the distortion parameters
        self.resetDistortionParameters()


    def resetDistortionParameters(self):
        """ Set the distortion parameters to zero. """

        # Distortion fit (forward and reverse)
        self.x_poly_fwd = np.zeros(shape=(12,), dtype=np.float64)
        self.y_poly_fwd = np.zeros(shape=(12,), dtype=np.float64)
        self.x_poly_rev = np.zeros(shape=(12,), dtype=np.float64)
        self.y_poly_rev = np.zeros(shape=(12,), dtype=np.float64)

        self.x_poly = self.x_poly_fwd
        self.y_poly = self.y_poly_fwd

        # Set the first coeffs to 0.5, as that is the real centre of the FOV
        self.x_poly_fwd[0] = 0.5
        self.y_poly_fwd[0] = 0.5
        self.x_poly_rev[0] = 0.5
        self.y_poly_rev[0] = 0.5



    def setDistortionType(self, distortion_type, reset_params=True):
        """ Sets the distortion type. """

        # List of distortion types an number of parameters for each
        self.distortion_type_list = [
             "poly3+radial",
                  "radial3",
                  "radial5",
                  "radial7"
            ]

        # Set the length of the distortion polynomial depending on the distortion type
        if distortion_type in self.distortion_type_list:
            self.distortion_type = distortion_type

        else:
            raise ValueError("The distortion type is not recognized: {:s}".format(self.distortion_type))


        # Reset distortion parameters
        if reset_params:
            self.resetDistortionParameters()


    def addVignettingCoeff(self, use_flat):
        """ Add a vignetting coeff to the platepar if it doesn't have one. 
        
        Arguments:
            use_flat: [bool] Is the flat used or not.
        """

        # Add a vignetting coefficient if it's not set
        if self.vignetting_coeff is None:

            # Only add it if a flat is not used
            if use_flat:
                self.vignetting_coeff = 0.0

            else:

                # Use 0.001 deg/px as the default coefficeint, as that's the one for 3.6 mm f/0.95 and 16 mm
                #   f/1.0 lenses. The vignetting coeff is dependent on the resolution, the default value of 
                #   0.001 deg/px is for 720p.
                self.vignetting_coeff = 0.001*np.hypot(1280, 720)/np.hypot(self.X_res, self.Y_res)



    def fitAstrometry(self, jd, img_stars, catalog_stars, first_platepar_fit=False):
        """ Fit astrometric parameters to the list of star image and celectial catalog coordinates. 
        At least 4 stars are needed to fit the rigid body parameters, and 12 to fit the distortion.
        New parameters are saved to the given object.

        Arguments:
            jd: [float] Julian date of the image.
            img_stars: [list] A list of (x, y, intensity_sum) entires for every star.
            catalog_stars: [list] A list of (ra, dec, mag) entries for every star (degrees).

        Keyword arguments:
            first_platepar_fit: [bool] Fit a platepar from scratch. False by default.

        """


        def _calcImageResidualsAstro(params, platepar, jd, catalog_stars, img_stars):
            """ Calculates the differences between the stars on the image and catalog stars in image 
                coordinates with the given astrometrical solution. 

            """

            # Extract fitting parameters
            ra_ref, dec_ref, pos_angle_ref, F_scale = params

            img_x, img_y, _ = img_stars.T

            pp_copy = copy.deepcopy(platepar)

            pp_copy.RA_d = ra_ref
            pp_copy.dec_d = dec_ref
            pp_copy.pos_angle_ref = pos_angle_ref
            pp_copy.F_scale = F_scale

            # Get image coordinates of catalog stars
            catalog_x, catalog_y, catalog_mag = getCatalogStarsImagePositions(catalog_stars, jd, pp_copy)
            
            # Calculate the sum of squared distances between image stars and catalog stars
            dist_sum = np.sum((catalog_x - img_x)**2 + (catalog_y - img_y)**2)

            return dist_sum


        def _calcSkyResidualsAstro(params, platepar, jd, catalog_stars, img_stars):
            """ Calculates the differences between the stars on the image and catalog stars in sky 
                coordinates with the given astrometrical solution. 

            """

            # Extract fitting parameters
            ra_ref, dec_ref, pos_angle_ref, F_scale = params

            pp_copy = copy.deepcopy(platepar)

            pp_copy.RA_d = ra_ref
            pp_copy.dec_d = dec_ref
            pp_copy.pos_angle_ref = pos_angle_ref
            pp_copy.F_scale = F_scale

            img_x, img_y, _ = img_stars.T

            # Get image coordinates of catalog stars
            ra_array, dec_array = getPairedStarsSkyPositions(img_x, img_y, jd, pp_copy)

            ra_catalog, dec_catalog, _ = catalog_stars.T

            # Compute the sum of the angular separation
            separation_sum = np.sum(angularSeparation(np.radians(ra_array), np.radians(dec_array), \
                np.radians(ra_catalog), np.radians(dec_catalog))**2)


            return separation_sum



        def _calcImageResidualsDistortion(params, platepar, jd, catalog_stars, img_stars, dimension):
            """ Calculates the differences between the stars on the image and catalog stars in image 
                coordinates with the given astrometrical solution. 

            Arguments:
                ...
                dimension: [str] 'x' for X polynomial fit, 'y' for Y polynomial fit

            """

            # Set distortion parameters
            pp_copy = copy.deepcopy(platepar)

            if dimension == 'x':
                pp_copy.x_poly_rev = params
                pp_copy.y_poly_rev = np.zeros(12)

            else:
                pp_copy.x_poly_rev = np.zeros(12)
                pp_copy.y_poly_rev = params


            img_x, img_y, _ = img_stars.T


            # Get image coordinates of catalog stars
            catalog_x, catalog_y, catalog_mag = getCatalogStarsImagePositions(catalog_stars, jd, pp_copy)


            # Calculate the sum of squared distances between image stars and catalog stars, per every
            #   dimension
            if dimension == 'x':
                dist_sum = np.sum((catalog_x - img_x)**2)

            else:
                dist_sum = np.sum((catalog_y - img_y)**2)


            return dist_sum


        def _calcSkyResidualsDistortion(params, platepar, jd, catalog_stars, img_stars, dimension):
            """ Calculates the differences between the stars on the image and catalog stars in sky 
                coordinates with the given astrometrical solution. 

            Arguments:
                ...
                dimension: [str] 'x' for X polynomial fit, 'y' for Y polynomial fit

            """

            pp_copy = copy.deepcopy(platepar)

            if dimension == 'x':
                pp_copy.x_poly_fwd = params

            else:
                pp_copy.y_poly_fwd = params


            img_x, img_y, _ = img_stars.T

            # Get image coordinates of catalog stars
            ra_array, dec_array = getPairedStarsSkyPositions(img_x, img_y, jd, pp_copy)

            ra_catalog, dec_catalog, _ = catalog_stars.T

            # Compute the sum of the angular separation
            separation_sum = np.sum(angularSeparation(np.radians(ra_array), np.radians(dec_array), \
                np.radians(ra_catalog), np.radians(dec_catalog))**2)

            return separation_sum


        # print('ASTRO', _calcImageResidualsAstro([self.RA_d, self.dec_d, 
        #     self.pos_angle_ref, self.F_scale],  catalog_stars, img_stars))

        # print('DIS_X', _calcImageResidualsDistortion(self.x_poly_rev,  catalog_stars, \
        #     img_stars, 'x'))

        # print('DIS_Y', _calcImageResidualsDistortion(self.y_poly_rev,  catalog_stars, \
        #     img_stars, 'y'))


        ### ASTROMETRIC PARAMETERS FIT ###

        # Initial parameters for the astrometric fit
        p0 = [self.RA_d, self.dec_d, self.pos_angle_ref, self.F_scale]

        # Fit the astrometric parameters using the reverse transform for reference        
        res = scipy.optimize.minimize(_calcImageResidualsAstro, p0, args=(self, jd, \
            catalog_stars, img_stars),
            method='Nelder-Mead')

        # # Fit the astrometric parameters using the forward transform for reference
        #   WARNING: USING THIS MAKES THE FIT UNSTABLE
        # res = scipy.optimize.minimize(_calcSkyResidualsAstro, p0, args=(self, jd, \
        #     catalog_stars, img_stars), method='Nelder-Mead')

        # Update fitted astrometric parameters
        self.RA_d, self.dec_d, self.pos_angle_ref, self.F_scale = res.x

        # Recalculate centre
        self.az_centre, self.alt_centre = RMS.Astrometry.ApplyAstrometry.raDec2AltAz(self.JD, self.lon, 
            self.lat, self.RA_d, self.dec_d)

        ### ###


        ### DISTORTION FIT ###

        # If there are more than 12 paired stars, fit the distortion parameters
        if len(img_stars) > 12:

            ### REVERSE MAPPING FIT ###
            # Fit distortion parameters in X direction, reverse mapping
            res = scipy.optimize.minimize(_calcImageResidualsDistortion, self.x_poly_rev, \
                args=(self, jd, catalog_stars, img_stars, 'x'), method='Nelder-Mead', \
                options={'maxiter': 10000, 'adaptive': True})

            # Exctact fitted X polynomial
            self.x_poly_rev = res.x


            # Only fit the Y polynomial if a polynomial fit is used, as in the radial fit all parameters
            #   are stored in the x_poly variable
            if "poly" in self.distortion_type:

                # Fit distortion parameters in Y direction, reverse mapping
                res = scipy.optimize.minimize(_calcImageResidualsDistortion, self.y_poly_rev, \
                    args=(self, jd, catalog_stars, img_stars, 'y'), method='Nelder-Mead', \
                    options={'maxiter': 10000, 'adaptive': True})

                # Extract fitted Y polynomial
                self.y_poly_rev = res.x

            ### ###

            

            # If this is the first fit of the distortion, set the forward parametrs to be equal to the reverse
            if first_platepar_fit:

                self.x_poly_fwd = np.array(self.x_poly_rev)
                self.y_poly_fwd = np.array(self.y_poly_rev)

                # Invert the first parameters
                self.x_poly_fwd[0] *= -1
                self.y_poly_fwd[0] *= -1



            ### FORWARD MAPPING FIT ###

            # Fit distortion parameters in X direction, forward mapping
            res = scipy.optimize.minimize(_calcSkyResidualsDistortion, self.x_poly_fwd, \
                args=(self, jd, catalog_stars, img_stars, 'x'), method='Nelder-Mead', \
                options={'maxiter': 10000, 'adaptive': True})

            # Exctact fitted X polynomial
            self.x_poly_fwd = res.x


            # Only fit the Y polynomial if a polynomial fit is used, as in the radial fit all parameters
            #   are stored in the x_poly variable
            if "poly" in self.distortion_type:

                # Fit distortion parameters in Y direction, forward mapping
                res = scipy.optimize.minimize(_calcSkyResidualsDistortion, self.y_poly_fwd, \
                    args=(self, jd, catalog_stars, img_stars, 'y'), method='Nelder-Mead', \
                    options={'maxiter': 10000, 'adaptive': True})

                # Extract fitted Y polynomial
                self.y_poly_fwd = res.x

            ### ###

        else:
            print('Too few stars to fit the distortion, only the astrometric parameters where fitted!')


        # Set the list of stars used for the fit to the platepar
        fit_star_list = []
        for img_coords, cat_coords in zip(img_stars, catalog_stars):

            # Store time, image coordinate x, y, intensity, catalog ra, dec, mag
            fit_star_list.append([jd] + img_coords.tolist() + cat_coords.tolist())


        self.star_list = fit_star_list


        # Set the flag to indicate that the platepar was manually fitted
        self.auto_check_fit_refined = False
        self.auto_recalibrated = False

        ### ###


    def parseLine(self, f):
        """ Read next line, split the line and convert parameters to float.

        @param f: [file handle] file we want to read

        @return (a1, a2, ...): [tuple of floats] parsed data from the line
        """

        return map(float, f.readline().split())


    def loadFromDict(self, platepar_dict, use_flat=None):
        """ Load the platepar from a dictionary. """

        # Parse JSON into an object with attributes corresponding to dict keys
        self.__dict__ = platepar_dict

        # Add the version if it was not in the platepar (v1 platepars didn't have a version)
        if not 'version' in self.__dict__:
            self.version = 1

        # Add the distortion type if not present (assume it's the polynomal type with the radial term)
        if not 'distortion_type' in self.__dict__:

            # Check if the variable with the typo was used and correct it
            if 'distortion_type' in self.__dict__:
                self.distortion_type = self.distortion_type
                del self.distortion_type

            # Otherwise, assume the polynomial type
            else:
                self.distortion_type = "poly3+radial"
        
        self.setDistortionType(self.distortion_type, reset_params=False)

        # Add UT correction if it was not in the platepar
        if not 'UT_corr' in self.__dict__:
            self.UT_corr = 0

        # Add the gamma if it was not in the platepar
        if not 'gamma' in self.__dict__:
            self.gamma = 1.0

        # Add the vignetting coefficient if it was not in the platepar
        if not 'vignetting_coeff' in self.__dict__:
            self.vignetting_coeff = None

            # Add the default vignetting coeff
            self.addVignettingCoeff(use_flat=use_flat)


        # Add the list of calibration stars if it was not in the platepar
        if not 'star_list' in self.__dict__:
            self.star_list = []

        # If v1 only the backward distortion coeffs were fitted, so use load them for both forward and
        #   reverse if nothing else is available
        if not 'x_poly_fwd' in self.__dict__:
            
            self.x_poly_fwd = np.array(self.x_poly)
            self.x_poly_rev = np.array(self.x_poly)
            self.y_poly_fwd = np.array(self.y_poly)
            self.y_poly_rev = np.array(self.y_poly)


        # Convert lists to numpy arrays
        self.x_poly_fwd = np.array(self.x_poly_fwd)
        self.x_poly_rev = np.array(self.x_poly_rev)
        self.y_poly_fwd = np.array(self.y_poly_fwd)
        self.y_poly_rev = np.array(self.y_poly_rev)

        # Set polynomial parameters used by the old code
        self.x_poly = self.x_poly_fwd
        self.y_poly = self.y_poly_fwd


        # Add rotation from horizontal
        if not 'rotation_from_horiz' in self.__dict__:
            self.rotation_from_horiz = RMS.Astrometry.ApplyAstrometry.rotationWrtHorizon(self)

        # Calculate the datetime
        self.time = jd2Date(self.JD, dt_obj=True)




    def read(self, file_name, fmt=None, use_flat=None):
        """ Read the platepar. 
            
        Arguments:
            file_name: [str] Path and the name of the platepar to read.

        Keyword arguments:
            fmt: [str] Format of the platepar file. 'json' for JSON format and 'txt' for the usual CMN textual
                format.
            use_flat: [bool] Indicates wheter a flat is used or not. None by default.

        Return:
            fmt: [str]
        """


        # Check if platepar exists
        if not os.path.isfile(file_name):
            return False


        # Determine the type of the platepar if it is not given
        if fmt is None:

            with open(file_name) as f:
                data = " ".join(f.readlines())

                # Try parsing the file as JSON
                try:
                    json.loads(data)
                    fmt = 'json'

                except:
                    fmt = 'txt'


        # Load the file as JSON
        if fmt == 'json':

            # Load the JSON file
            with open(file_name) as f:
                data = " ".join(f.readlines())

            # Load the platepar from the JSON dictionary
            self.loadFromDict(json.loads(data), use_flat=use_flat)




        # Load the file as TXT (old CMN format)
        else:

            with open(file_name) as f:

                self.UT_corr = 0
                self.gamma = 1.0
                self.star_list = []

                # Parse latitude, longitude, elevation
                self.lon, self.lat, self.elev = self.parseLine(f)

                # Parse date and time as int
                D, M, Y, h, m, s = map(int, f.readline().split())

                # Calculate the datetime of the platepar time
                self.time = datetime.datetime(Y, M, D, h, m, s)

                # Convert time to JD
                self.JD = date2JD(Y, M, D, h, m, s)

                # Calculate the reference hour angle
                T = (self.JD - 2451545.0)/36525.0
                self.Ho = (280.46061837 + 360.98564736629*(self.JD - 2451545.0) + 0.000387933*T**2 - 
                    T**3/38710000.0)%360

                # Parse camera parameters
                self.X_res, self.Y_res, self.focal_length = self.parseLine(f)

                # Parse the right ascension of the image centre
                self.RA_d, self.RA_H, self.RA_M, self.RA_S = self.parseLine(f)

                # Parse the declination of the image centre
                self.dec_d, self.dec_D, self.dec_M, self.dec_S = self.parseLine(f)

                # Parse the rotation parameter
                self.pos_angle_ref = self.parseLine(f)[0]

                # Parse the image scale (convert from arcsec/px to px/deg)
                self.F_scale = self.parseLine(f)[0]
                self.F_scale = 3600/self.F_scale

                # Load magnitude slope parameters
                self.mag_0, self.mag_lev = self.parseLine(f)

                # Load X axis polynomial parameters
                self.x_poly_fwd = self.x_poly_rev = np.zeros(shape=(12,), dtype=np.float64)
                for i in range(12):
                    self.x_poly_fwd[i] = self.x_poly_fwd[i] = self.parseLine(f)[0]

                # Load Y axis polynomial parameters
                self.y_poly_fwd = self.y_poly_rev = np.zeros(shape=(12,), dtype=np.float64)
                for i in range(12):
                    self.y_poly_fwd[i] = self.y_poly_rev[i] = self.parseLine(f)[0]

                # Read station code
                self.station_code = f.readline().replace('\r', '').replace('\n', '')


        # Add a default vignetting coefficient if it already doesn't exist
        self.addVignettingCoeff(use_flat)

        return fmt


    def jsonStr(self):
        """ Returns the JSON representation of the platepar as a string. """

        # Make a copy of the platepar object, which will be modified for writing
        self2 = copy.deepcopy(self)

        # Convert numpy arrays to list, which can be serialized
        self2.x_poly_fwd = self.x_poly_fwd.tolist()
        self2.x_poly_rev = self.x_poly_rev.tolist()
        self2.y_poly_fwd = self.y_poly_fwd.tolist()
        self2.y_poly_rev = self.y_poly_rev.tolist()
        del self2.time

        # For compatibility with old procedures, write the forward distortion parameters as x, y
        self2.x_poly = self.x_poly_fwd.tolist()
        self2.y_poly = self.y_poly_fwd.tolist()

        out_str = json.dumps(self2, default=lambda o: o.__dict__, indent=4, sort_keys=True)

        return out_str


    def write(self, file_path, fmt=None, fov=None, ret_written=False):
        """ Write platepar to file. 
        
        Arguments:
            file_path: [str] Path and the name of the platepar to write.

        Keyword arguments:
            fmt: [str] Format of the platepar file. 'json' for JSON format and 'txt' for the usual CMN textual
                format. The format is JSON by default.
            fov: [tuple] Tuple of horizontal and vertical FOV size in degree. None by default.
            ret_written: [bool] If True, the JSON string of the platepar instead of writing it to disk.

        Return:
            fmt: [str] Platepar format.

        """

        # If the FOV size was given, store it
        if fov is not None:
            self.fov_h, self.fov_v = fov


        # Set JSON to be the defualt format
        if fmt is None:
            fmt = 'json'


        # If the format is JSON, write a JSON file
        if fmt == 'json':

            out_str = self.jsonStr()

            with open(file_path, 'w') as f:
                f.write(out_str)

            if ret_written:
                return fmt, out_str


        # Old CMN format
        else:

            with open(file_path, 'w') as f:
                
                # Write geo coords
                f.write('{:9.6f} {:9.6f} {:04d}\n'.format(self.lon, self.lat, int(self.elev)))

                # Calculate reference time from reference JD
                Y, M, D, h, m, s, ms = list(map(int, jd2Date(self.JD)))

                # Write the reference time
                f.write('{:02d} {:02d} {:04d} {:02d} {:02d} {:02d}\n'.format(D, M, Y, h, m, s))

                # Write resolution and focal length
                f.write('{:d} {:d} {:f}\n'.format(int(self.X_res), int(self.Y_res), self.focal_length))

                # Write reference RA
                self.RA_H = int(self.RA_d/15)
                self.RA_M = int((self.RA_d/15 - self.RA_H)*60)
                self.RA_S = int(((self.RA_d/15 - self.RA_H)*60 - self.RA_M)*60)

                f.write("{:7.3f} {:02d} {:02d} {:02d}\n".format(self.RA_d, self.RA_H, self.RA_M, self.RA_S))

                # Write reference Dec
                self.dec_D = int(self.dec_d)
                self.dec_M = int((self.dec_d - self.dec_D)*60)
                self.dec_S = int(((self.dec_d - self.dec_D)*60 - self.dec_M)*60)

                f.write("{:+7.3f} {:02d} {:02d} {:02d}\n".format(self.dec_d, self.dec_D, self.dec_M, self.dec_S))

                # Write rotation parameter
                f.write('{:<7.3f}\n'.format(self.pos_angle_ref))

                # Write F scale
                f.write('{:<5.1f}\n'.format(3600/self.F_scale))

                # Write magnitude fit
                f.write("{:.3f} {:.3f}\n".format(self.mag_0, self.mag_lev))

                # Write X distortion polynomial
                for x_elem in self.x_poly_fwd:
                    f.write('{:+E}\n'.format(x_elem))

                # Write y distortion polynomial
                for y_elem in self.y_poly_fwd:
                    f.write('{:+E}\n'.format(y_elem))

                # Write station code
                f.write(str(self.station_code) + '\n')

            if ret_written:
                with open(file_path) as f:
                    out_str = "\n".join(f.readlines())
    
                return fmt, out_str


        return fmt


    def __repr__(self):
        return str(self.__dict__)



if __name__ == "__main__":


    # Platepar file
    pp_file = "/home/dvida/Desktop/platepar_cmn2010.cal"

    pp = Platepar()
    pp.read(pp_file)

    print(pp.x_poly_fwd)
    print(pp.station_code)

    #txt = json.dumps(pp, default=lambda o: o.__dict__)

    pp.write(pp_file + '.json')