import argparse
import time
import msgpack
from enum import Enum, auto
import numpy.linalg as LA

import numpy as np

from queue import PriorityQueue
from planning_utils import a_star, heuristic, lat_lon, create_grid, collides, extract_polygons,find_start_goal, collinearity
from planning_utils import bres, coll, heading
from skimage.morphology import medial_axis
from skimage.util import invert

from udacidrone import Drone
from udacidrone.connection import MavlinkConnection
from udacidrone.messaging import MsgID
from udacidrone.frame_utils import global_to_local

import sys



class States(Enum):
    MANUAL = auto()
    ARMING = auto()
    TAKEOFF = auto()
    WAYPOINT = auto()
    LANDING = auto()
    DISARMING = auto()
    PLANNING = auto()


class MotionPlanning(Drone):

    def __init__(self, connection):
        super().__init__(connection)

        self.target_position = np.array([0.0, 0.0, 0.0])
        self.waypoints = []
        self.in_mission = True
        self.check_state = {}

        # initial state
        self.flight_state = States.MANUAL

        # register all your callbacks here
        self.register_callback(MsgID.LOCAL_POSITION, self.local_position_callback)
        self.register_callback(MsgID.LOCAL_VELOCITY, self.velocity_callback)
        self.register_callback(MsgID.STATE, self.state_callback)

    def local_position_callback(self):
        if self.flight_state == States.TAKEOFF:
             if -1.0 * self.local_position[2] > 0.95 * self.target_position[2]:
                self.waypoint_transition()
        elif self.flight_state == States.WAYPOINT:
            if np.linalg.norm(self.target_position[0:2] - self.local_position[0:2]) < (np.linalg.norm(self.local_velocity[0:2]) + 1):
                if len(self.waypoints) > 0:
                    self.waypoint_transition()
                else:
                    if np.linalg.norm(self.local_velocity[0:2]) < 1.0:
                        self.landing_transition()

    def velocity_callback(self):
        if self.flight_state == States.LANDING:
            if abs(self.local_position[2]) < (self.goal_altitude - 5 + .01):
                self.disarming_transition()   

    def state_callback(self):
        if self.in_mission:
            if self.flight_state == States.MANUAL:
                self.arming_transition()
            elif self.flight_state == States.ARMING:
                if self.armed:
                    self.plan_path()
            elif self.flight_state == States.PLANNING:
                self.takeoff_transition()
            elif self.flight_state == States.DISARMING:
                if ~self.armed & ~self.guided:
                    self.manual_transition()

    def arming_transition(self):
        self.flight_state = States.ARMING
        print("arming transition")
        self.arm()
        self.take_control()

    def takeoff_transition(self):
        self.flight_state = States.TAKEOFF
        print("takeoff transition")
        self.takeoff(self.target_position[2])

    def waypoint_transition(self):
        self.flight_state = States.WAYPOINT
        print("waypoint transition")
        self.target_position = self.waypoints.pop(0)
        print('target position', self.target_position)
        self.cmd_position(self.target_position[0], self.target_position[1], self.target_position[2], self.target_position[3])

    def landing_transition(self):
        self.flight_state = States.LANDING
        print("landing transition")
        self.land()

    def disarming_transition(self):
        self.flight_state = States.DISARMING
        print("disarm transition")
        self.disarm()
        self.release_control()

    def manual_transition(self):
        self.flight_state = States.MANUAL
        print("manual transition")
        self.stop()
        self.in_mission = False

    def send_waypoints(self):
        print("Sending waypoints to simulator ...")
        data = msgpack.dumps(self.waypoints)
        self.connection._master.write(data)

    def plan_path(self):
        self.flight_state = States.PLANNING
        print("Searching for a path ...")
    

        #Read lat0, lon0 from 'colliders' obstacle data into floating point values
        lat0, lon0 = lat_lon('colliders.csv')
        print('Home Lattidute: {0}, Home Longitude: {1}'.format(lat0, lon0))

        self.set_home_position = (lon0, lat0, 0)

        # Read in obstacle map
        data = np.loadtxt('colliders.csv', delimiter=',', dtype='Float64', skiprows=2)

        
        # minimum and maximum of obstacle coordinates
        north_min = np.floor(np.min(data[:, 0] - data[:, 3]))
        east_min = np.floor(np.min(data[:, 1] - data[:, 4]))
        north_max = np.ceil(np.max(data[:, 0] + data[:, 3]))
        east_max = np.ceil(np.max(data[:, 1] + data[:, 4]))
        
        
        start_latlon = [self._longitude, self._latitude, self._altitude]
        start = global_to_local(start_latlon, self.global_home)
        print("Local start location")
        print(start)
        
        
        #INPUT GOAL AS LONGITUDE/LATITUDE COORIDNATES
        goal_lat_lon = (-122.394813, 37.793815)
        goal_latlon = [goal_lat_lon[0], goal_lat_lon[1], 0]


        #Goal location converted to local coordinate frame
        goal = global_to_local(goal_latlon, self.global_home)
        print("Local goal location")
        print(goal)
        
        if (goal[0] < north_min or goal[0] > north_max) or (goal[1] < east_min or goal[1] > east_max):
            self.disarming_transition()
            sys.exit("Goal location is off the grid!")
        
        if LA.norm(goal[0:2] - start[0:2]) < 5:
            self.disarming_transition()
            sys.exit("Goal location is the same as start!")

        #Check if the goal location is on a roof or the ground
        polygons = extract_polygons(data)
        does_collide = collides(polygons, goal)
        if does_collide[0] == True:
            print("Landing on a roof!")
            TARGET_ALTITUDE = does_collide[1] + 5
            self.goal_altitude = does_collide[1] + 5
        else:
            print("Landing on the ground!")
            TARGET_ALTITUDE = 5
            self.goal_altitude = 5
        
        if TARGET_ALTITUDE < (-self.local_position[2] + 5):
            TARGET_ALTITUDE = int(-self.local_position[2] + 5)
            
        SAFETY_DISTANCE = 5
        self.target_position[2] = TARGET_ALTITUDE
        print('Target altitude set to {0} meters'.format(TARGET_ALTITUDE))

        #Create a grid with obstacle data
        grid = create_grid(data, TARGET_ALTITUDE, SAFETY_DISTANCE)
        skeleton = medial_axis(invert(grid))

        #Find a start.goal location on the skeleton
        skel_start, skel_goal = find_start_goal(skeleton, [start[0] - north_min, start[1] - east_min], [goal[0] - north_min, goal[1] - east_min])

        #Find a path on the skeleton
        path, cost, found = a_star(invert(skeleton).astype(np.int), heuristic, tuple(skel_start), tuple(skel_goal))
        if found == False:
            self.disarming_transition()
            sys.exit("No path was found!")
        path.append((int(goal[0]) - int(north_min), int(goal[1]) - int(east_min)))

        if len(path) > 0:
            print("Pruning Waypoints")
            #Use collinearity, bresenham to prune path
            path = coll(path)
            path = bres(path, grid)

            #Find heading for each waypoint
            path = heading(path)
            
            # Convert path to waypoints
            waypoints = [[p[0] + int(north_min), p[1] + int(east_min), TARGET_ALTITUDE, p[2]] for p in path]
            print("Number of waypoints: ", len(waypoints))
            print("Waypoints: ")
            print(waypoints)
        self.waypoints = waypoints
        print("set wayponits")

    def start(self):
        self.start_log("Logs", "NavLog.txt")

        print("starting connection")
        self.connection.start()
        
        self.stop_log()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5760, help='Port number')
    parser.add_argument('--host', type=str, default='127.0.0.1', help="host address, i.e. '127.0.0.1'")
    args = parser.parse_args()

    conn = MavlinkConnection('tcp:{0}:{1}'.format(args.host, args.port), timeout=60)
    drone = MotionPlanning(conn)
    time.sleep(1)

    drone.start()
