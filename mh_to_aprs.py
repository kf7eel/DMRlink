#! /usr/bin/python3


# This script will return coordinates for APRS based on maidenhead grid square.
# This was hacked together as Python2 will not run maidenhead module.

import sys
import maidenhead as mh
import re
import sys
def decdeg2dms(dd):
   is_positive = dd >= 0
   dd = abs(dd)
   minutes,seconds = divmod(dd*3600,60)
   degrees,minutes = divmod(minutes,60)
   degrees = degrees if is_positive else -degrees
   return (degrees,minutes,seconds)

#grid_square = 'cn97uk'
grid_square = str(sys.argv[1])
lat_lon = mh.to_location(grid_square)
#print(lat_lon)
#print(mh.to_location(str(grid_square)))


lat = decdeg2dms(mh.to_location(grid_square)[0])
lon = decdeg2dms(mh.to_location(grid_square)[1])

if lon[0] < 0:
    lon_dir = 'W'
if lon[0] > 0:
    lon_dir = 'E'
if lat[0] < 0:
    lat_dir = 'S'
if lat[0] > 0:
    lat_dir = 'N'
#logger.info(lat)
#logger.info(lat_dir)
aprs_lat = str(str(re.sub('\..*|-', '', str(lat[0]))) + str(re.sub('\..*', '', str(lat[1])) + '.').ljust(5) + lat_dir)
aprs_lon = str(str(re.sub('\..*|-', '', str(lon[0]))) + str(re.sub('\..*', '', str(lon[1])) + '.').ljust(5) + lon_dir)

print("('" + aprs_lat + "', '" + aprs_lon + "')")

