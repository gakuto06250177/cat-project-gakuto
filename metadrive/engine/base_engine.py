import logging
import pickle
import time
from collections import OrderedDict
from typing import Callable, Optional, Union, List, Dict, AnyStr

import numpy as np
from panda3d.core import NodePath, Vec3

from metadrive.base_class.randomizable import Randomizable
from metadrive.engine.core.engine_core import EngineCore
from metadrive.engine.interface import Interface
from metadrive.manager.base_manager import BaseManager
from metadrive.utils import concat_step_infos
from metadrive.utils.utils import is_map_related_class

logger = logging.getLogger(__name__)


class BaseEngine(EngineCore, Randomizable):
    """
    Due to the feature of Panda3D, BaseEngine should only be created once(Singleton Pattern)
    It is a pure game engine, which is not task-specific, while BaseEngine connects the
    driving task and the game engine modified from Panda3D Engine.
    """
    singleton = None
    global_random_seed = None

    def __init__(self, global_config):
        EngineCore.__init__(self, global_config)
        Randomizable.__init__(self, self.global_random_seed)
        self.episode_step = 0
        BaseEngine.singleton = self
        self.interface = Interface(self)

        # managers
        self.task_manager = self.taskMgr  # use the inner TaskMgr of Panda3D as MetaDrive task manager
        self._managers = OrderedDict()

        # for recovering, they can not exist together
        self.record_episode = False
        self.replay_episode = False
        self.only_reset_when_replay = False
        # self.accept("s", self._stop_replay)

        # cull scene
        self.cull_scene = self.global_config["cull_scene"]

        # add camera or not
        self.main_camera = self.setup_main_camera()

        self._spawned_objects = dict()
        self._object_policies = dict()
        self._object_tasks = dict()

        # the clear function is a fake clear, objects cleared is stored for future use
        self._dying_objects = dict()

        # store external actions
        self.external_actions = None

        # topdown renderer
        self._top_down_renderer = None

        # lanes debug
        self.lane_coordinates_debug_node = None

        # warm up
        self.warmup()

        # for multi-thread rendering
        self.graphicsEngine.renderFrame()
        self.graphicsEngine.renderFrame()

    def add_policy(self, object_id, policy_class, *args, **kwargs):
        policy = policy_class(*args, **kwargs)
        self._object_policies[object_id] = policy
        if self.record_episode:
            assert self.record_manager is not None, "No record manager"
            self.record_manager.add_policy_info(object_id, policy_class, *args, **kwargs)
        return policy

    def add_task(self, object_id, task):
        self._object_tasks[object_id] = task

    def get_policy(self, object_id):
        """
        Return policy of specific object with id
        :param object_id: a filter function, only return objects satisfying this condition
        :return: policy
        """
        if object_id in self._object_policies:
            return self._object_policies[object_id]
        else:
            # print("Can not find the policy for object(id: {})".format(object_id))
            return None

    def get_task(self, object_id):
        """
        Return task of specific object with id
        :param object_id: a filter function, only return objects satisfying this condition
        :return: task
        """
        assert object_id in self._object_tasks, "Can not find the task for object(id: {})".format(object_id)
        return self._object_tasks[object_id]

    def has_policy(self, object_id, policy_cls=None):
        if policy_cls is None:
            return True if object_id in self._object_policies else False
        else:
            return True if object_id in self._object_policies and isinstance(
                self._object_policies[object_id], policy_cls
            ) else False

    def has_task(self, object_id):
        return True if object_id in self._object_tasks else False

    def spawn_object(
        self, object_class, pbr_model=True, force_spawn=False, auto_fill_random_seed=True, record=True, **kwargs
    ):
        """
        Call this func to spawn one object
        :param object_class: object class
        :param pbr_model: if the visualization model is pbr model
        :param force_spawn: spawn a new object instead of fetching from _dying_objects list
        :param auto_fill_random_seed: whether to set random seed using purely random integer
        :param record: record the spawn information
        :param kwargs: class init parameters
        :return: object spawned
        """
        if ("random_seed" not in kwargs) and auto_fill_random_seed:
            kwargs["random_seed"] = self.generate_seed()
        if force_spawn or object_class.__name__ not in self._dying_objects or len(
                self._dying_objects[object_class.__name__]) == 0:
            obj = object_class(**kwargs)
        else:
            obj = self._dying_objects[object_class.__name__].pop()
            obj.reset(**kwargs)
            if not is_map_related_class(object_class) and ("name" not in kwargs or kwargs["name"] is None):
                obj.random_rename()

        if "name" in kwargs and kwargs["name"] is not None:
            assert kwargs["name"] == obj.name == obj.id
        if "id" in kwargs and kwargs["name"] is not None:
            assert kwargs["id"] == obj.id == obj.name

        if self.global_config["record_episode"] and not self.replay_episode and record:
            self.record_manager.add_spawn_info(obj, object_class, kwargs)
        self._spawned_objects[obj.id] = obj
        obj.attach_to_world(self.pbr_worldNP if pbr_model else self.worldNP, self.physics_world)
        return obj

    def get_objects(self, filter: Optional[Union[Callable, List]] = None):
        """
        Return objects spawned, default all objects. Filter_func will be applied on all objects.
        It can be a id list or a function
        Since we don't expect a iterator, and the number of objects is not so large, we don't use built-in filter()
        :param filter: a filter function, only return objects satisfying this condition
        :return: return all objects or objects satisfying the filter_func
        """
        if filter is None:
            return self._spawned_objects
        elif isinstance(filter, (list, tuple)):
            return {id: self._spawned_objects[id] for id in filter}
        elif callable(filter):
            res = dict()
            for id, obj in self._spawned_objects.items():
                if filter(obj):
                    res[id] = obj
            return res
        else:
            raise ValueError("filter should be a list or a function")

    def get_policies(self):
        """
        Return a mapping from object ID to policy instance.
        """
        return self._object_policies

    def get_object(self, object_id):
        return self.get_objects([object_id])

    def clear_objects(self, filter: Optional[Union[Callable, List]], force_destroy=False, record=True):
        """
        Destroy all self-generated objects or objects satisfying the filter condition
        Since we don't expect a iterator, and the number of objects is not so large, we don't use built-in filter()
        If force_destroy=True, we will destroy this element instead of storing them for next time using

        filter: A list of object ids or a function returning a list of object id
        """
        force_destroy_this_obj = True if force_destroy or self.global_config["force_destroy"] else False

        if isinstance(filter, (list, tuple)):
            exclude_objects = {obj_id: self._spawned_objects[obj_id] for obj_id in filter}
        elif callable(filter):
            exclude_objects = dict()
            for id, obj in self._spawned_objects.items():
                if filter(obj):
                    exclude_objects[id] = obj
        else:
            raise ValueError("filter should be a list or a function")
        for id, obj in exclude_objects.items():
            self._spawned_objects.pop(id)
            if id in self._object_tasks:
                self._object_tasks.pop(id)
            if id in self._object_policies:
                policy = self._object_policies.pop(id)
                policy.destroy()
            if force_destroy_this_obj:
                obj.destroy()
            else:
                obj.detach_from_world(self.physics_world)

                # We might want to remove some episode-relevant information when recycling some objects
                if hasattr(obj, "before_reset"):
                    obj.before_reset()

                if obj.class_name not in self._dying_objects:
                    self._dying_objects[obj.class_name] = []
                # We have a limit for buffering objects
                if len(self._dying_objects[obj.class_name]) < self.global_config["num_buffering_objects"]:
                    self._dying_objects[obj.class_name].append(obj)
                else:
                    obj.destroy()
            if self.global_config["record_episode"] and not self.replay_episode and record:
                self.record_manager.add_clear_info(obj)
        return exclude_objects.keys()

    def clear_object_if_possible(self, obj, force_destroy):
        if isinstance(obj, dict):
            return
        if obj in self._spawned_objects:
            self.clear_objects([obj], force_destroy=force_destroy)
        if force_destroy and \
                obj.class_name in self._dying_objects and \
                obj in self._dying_objects[obj.class_name]:
            self._dying_objects[obj.class_name].remove(obj)
            if hasattr(obj, "destroy"):
                obj.destroy()
        del obj

    def reset(self):
        """
        Clear and generate the whole scene
        """
        # initialize
        self._episode_start_time = time.time()
        self.episode_step = 0
        if self.global_config["debug_physics_world"]:
            self.addTask(self.report_body_nums, "report_num")

        # Update record replay
        self.replay_episode = True if self.global_config["replay_episode"] is not None else False
        self.record_episode = self.global_config["record_episode"]
        self.only_reset_when_replay = self.global_config["only_reset_when_replay"]

        _debug_memory_usage = False

        if _debug_memory_usage:

            def process_memory():
                import psutil
                import os
                process = psutil.Process(os.getpid())
                mem_info = process.memory_info()
                return mem_info.rss

            cm = process_memory()

        # reset manager
        for manager_name, manager in self._managers.items():
            # clean all manager
            manager.before_reset()

            if _debug_memory_usage:
                lm = process_memory()
                if lm - cm != 0:
                    print("{}: Before Reset! Mem Change {:.3f}MB".format(manager_name, (lm - cm) / 1e6))
                cm = lm

        self._object_clean_check()

        for manager_name, manager in self.managers.items():
            if self.replay_episode and self.only_reset_when_replay and manager is not self.replay_manager:
                # The scene will be generated from replay manager in only reset replay mode
                continue
            manager.reset()

            if _debug_memory_usage:
                lm = process_memory()
                if lm - cm != 0:
                    print("{}: Reset! Mem Change {:.3f}MB".format(manager_name, (lm - cm) / 1e6))
                cm = lm

        for manager_name, manager in self.managers.items():
            manager.after_reset()

            if _debug_memory_usage:
                lm = process_memory()
                if lm - cm != 0:
                    print("{}: After Reset! Mem Change {:.3f}MB".format(manager_name, (lm - cm) / 1e6))
                cm = lm

        # reset cam
        if self.main_camera is not None:
            self.main_camera.reset()
            if hasattr(self, "agent_manager"):
                bev_cam = self.main_camera.is_bird_view_camera() and self.main_camera.current_track_vehicle is not None
                vehicles = list(self.agents.values())
                current_track_vehicle = vehicles[0]
                self.main_camera.set_follow_lane(self.global_config["use_chase_camera_follow_lane"])
                self.main_camera.track(current_track_vehicle)
                if bev_cam:
                    self.main_camera.stop_track()
                    self.main_camera.set_bird_view_pos(current_track_vehicle.position)

                # if self.global_config["is_multi_agent"]:
                #     self.main_camera.stop_track(bird_view_on_current_position=False)

        # reset terrain
        center_p = self.current_map.get_center_point()
        self.terrain.reset(center_p)
        if self.sky_box is not None:
            self.sky_box.set_position(center_p)

        self.taskMgr.step()

    def before_step(self, external_actions: Dict[AnyStr, np.array]):
        """
        Entities make decision here, and prepare for step
        All entities can access this global manager to query or interact with others
        :param external_actions: Dict[agent_id:action]
        :return:
        """
        self.episode_step += 1
        step_infos = {}
        self.external_actions = external_actions
        for manager in self.managers.values():
            new_step_infos = manager.before_step()
            step_infos = concat_step_infos([step_infos, new_step_infos])
        return step_infos

    def step(self, step_num: int = 1) -> None:
        """
        Step the dynamics of each entity on the road.
        :param step_num: Decision of all entities will repeat *step_num* times
        """
        for i in range(step_num):
            # simulate or replay
            for name, manager in self.managers.items():
                if name != "record_manager":
                    manager.step()
            self.step_physics_world()
            # the recording should happen after step physics world
            if "record_manager" in self.managers and i < step_num - 1:
                # last recording should be finished in after_step(), as some objects may be created in after_step.
                # We repeat run simulator ```step_num``` frames, and record after each frame.
                # The recording of last frame is actually finished when all managers finish the ```after_step()```
                # function. So the recording for the last time should be done after that.
                # An example is that in ```PGTrafficManager``` we may create new vehicles in
                # ```after_step()``` of the traffic manager. Therefore, we can't record the frame before that.
                # These new cars' states can be recorded only if we run ```record_managers.step()```
                # after the creation of new cars and then can be recorded in ```record_managers.after_step()```
                self.record_manager.step()

            if self.force_fps.real_time_simulation and i < step_num - 1:
                self.task_manager.step()
        #  panda3d render and garbage collecting loop
        self.task_manager.step()
        if self.on_screen_message is not None:
            self.on_screen_message.render()

    def after_step(self, *args, **kwargs) -> Dict:
        """
        Update states after finishing movement
        :return: if this episode is done
        """

        step_infos = {}
        if self.record_episode:
            assert list(self.managers.keys())[-1] == "record_manager", "Record Manager should have lowest priority"
        for manager in self.managers.values():
            new_step_info = manager.after_step(*args, **kwargs)
            step_infos = concat_step_infos([step_infos, new_step_info])
        self.interface.after_step()

        # === Option 1: Set episode_step to "num of calls to env.step"
        # We want to make sure that the episode_step is always aligned to the "number of calls to env.step"
        # So if this function is called in env.reset, we will not increment episode_step.
        # if call_from_reset:
        #     pass
        # else:
        #     self.episode_step += 1

        # === Option 2: Following old code.
        # Note that this function will be called in _get_reset_return.
        # Therefore, after reset the episode_step is immediately goes to 1
        # even if no env.step is called.

        # Episode_step should be increased before env.step(). I moved it to engine.before_step() now.

        # cull distant blocks
        # poses = [v.position for v in self.agent_manager.active_agents.values()]
        # if self.cull_scene and False:
        #     SceneCull.cull_distant_blocks(self, self.current_map.blocks, poses, self.global_config["max_distance"])
        return step_infos

    def dump_episode(self, pkl_file_name=None) -> None:
        """Dump the data of an episode."""
        assert self.record_manager is not None
        episode_state = self.record_manager.get_episode_metadata()
        if pkl_file_name is not None:
            with open(pkl_file_name, "wb+") as file:
                pickle.dump(episode_state, file)
        return episode_state

    def close(self):
        """
        Note:
        Instead of calling this func directly, close Engine by using engine_utils.close_engine
        """
        if len(self._managers) > 0:
            for name, manager in self._managers.items():
                setattr(self, name, None)
                if manager is not None:
                    manager.destroy()
        # clear all objects in spawned_object
        # self.clear_objects([id for id in self._spawned_objects.keys()])
        for id, obj in self._spawned_objects.items():
            if id in self._object_policies:
                self._object_policies.pop(id).destroy()
            if id in self._object_tasks:
                self._object_tasks.pop(id).destroy()
            obj.destroy()
        for cls, pending_obj in self._dying_objects.items():
            for obj in pending_obj:
                obj.destroy()
        if self.main_camera is not None:
            self.main_camera.destroy()
        self.interface.destroy()
        self.close_world()

        if self._top_down_renderer is not None:
            self._top_down_renderer.close()
            del self._top_down_renderer
            self._top_down_renderer = None

    def __del__(self):
        logging.debug("{} is destroyed".format(self.__class__.__name__))

    def _stop_replay(self):
        raise DeprecationWarning
        if not self.IN_REPLAY:
            return
        self.STOP_REPLAY = not self.STOP_REPLAY

    def register_manager(self, manager_name: str, manager: BaseManager):
        """
        Add a manager to BaseEngine, then all objects can communicate with this class
        :param manager_name: name shouldn't exist in self._managers and not be same as any class attribute
        :param manager: subclass of BaseManager
        """
        assert manager_name not in self._managers, "Manager already exists in BaseEngine, Use update_manager() to " \
                                                   "overwrite"
        assert not hasattr(self, manager_name), "Manager name can not be same as the attribute in BaseEngine"
        self._managers[manager_name] = manager
        setattr(self, manager_name, manager)
        self._managers = OrderedDict(sorted(self._managers.items(), key=lambda k_v: k_v[-1].PRIORITY))

    def seed(self, random_seed):
        self.global_random_seed = random_seed
        super(BaseEngine, self).seed(random_seed)
        for mgr in self._managers.values():
            mgr.seed(random_seed)

    @property
    def current_map(self):
        if self.replay_episode:
            return self.replay_manager.current_map
        else:
            if hasattr(self, "map_manager"):
                return self.map_manager.current_map
            else:
                return None

    @property
    def current_track_vehicle(self):
        if self.main_camera is not None:
            return self.main_camera.current_track_vehicle
        elif "default_agent" in self.agents:
            return self.agents["default_agent"]
        else:
            return None

    @property
    def agents(self):
        if not self.replay_episode:
            return self.agent_manager.active_agents
        else:
            return self.replay_manager.replay_agents

    def setup_main_camera(self):
        from metadrive.engine.core.main_camera import MainCamera
        # Not we should always enable main camera if image obs is required! Or RGBCamera will return incorrect result
        if self.global_config["use_render"] or self.global_config["image_observation"]:
            return MainCamera(self, self.global_config["camera_height"], self.global_config["camera_dist"])
        else:
            return None

    @property
    def current_seed(self):
        return self.global_random_seed

    @property
    def global_seed(self):
        return self.global_random_seed

    def _object_clean_check(self):
        if self.global_config["debug"]:
            from metadrive.component.vehicle.base_vehicle import BaseVehicle
            from metadrive.component.static_object.traffic_object import TrafficObject
            for manager in self._managers.values():
                assert len(manager.spawned_objects) == 0

            objs_need_to_release = self.get_objects(
                filter=lambda obj: isinstance(obj, BaseVehicle) or isinstance(obj, TrafficObject)
            )
            assert len(
                objs_need_to_release) == 0, "You should clear all generated objects by using engine.clear_objects " \
                                            "in each manager.before_step()"

    def update_manager(self, manager_name: str, manager: BaseManager, destroy_previous_manager=True):
        """
        Update an existing manager with a new one
        :param manager_name: existing manager name
        :param manager: new manager
        """
        assert manager_name in self._managers, "You may want to call register manager, since {} is not in engine".format(
            manager_name
        )
        existing_manager = self._managers.pop(manager_name)
        if destroy_previous_manager:
            existing_manager.destroy()
        self._managers[manager_name] = manager
        setattr(self, manager_name, manager)
        self._managers = OrderedDict(sorted(self._managers.items(), key=lambda k_v: k_v[-1].PRIORITY))

    @property
    def managers(self):
        # whether to froze other managers
        return {"replay_manager": self.replay_manager} if self.replay_episode and not \
            self.only_reset_when_replay else self._managers

    def change_object_name(self, obj, new_name):
        raise DeprecationWarning("This function is too dangerous to be used")
        """
        Change the name of one object, Note: it may bring some bugs if abusing
        """
        obj = self._spawned_objects.pop(obj.name)
        self._spawned_objects[new_name] = obj

    def object_to_agent(self, obj_name):
        if self.replay_episode:
            return self.replay_manager.current_frame.object_to_agent(obj_name)
        else:
            return self.agent_manager.object_to_agent(obj_name)

    def agent_to_object(self, agent_name):
        if self.replay_episode:
            return self.replay_manager.current_frame.agent_to_object(agent_name)
        else:
            return self.agent_manager.agent_to_object(agent_name)

    def render_topdown(self, text, *args, **kwargs):
        if self._top_down_renderer is None:
            from metadrive.obs.top_down_renderer import TopDownRenderer
            self._top_down_renderer = TopDownRenderer(*args, **kwargs)
        return self._top_down_renderer.render(text, *args, **kwargs)

    def get_window_image(self, return_bytes=False):
        window_count = self.graphicsEngine.getNumWindows() - 1
        texture = self.graphicsEngine.getWindow(window_count).getDisplayRegion(0).getScreenshot()

        assert texture.getXSize() == self.global_config["window_size"][0], (
            texture.getXSize(), texture.getYSize(), self.global_config["window_size"]
        )
        assert texture.getYSize() == self.global_config["window_size"][1], (
            texture.getXSize(), texture.getYSize(), self.global_config["window_size"]
        )

        image_bytes = texture.getRamImage().getData()

        if return_bytes:
            return image_bytes, (texture.getXSize(), texture.getYSize())

        img = np.frombuffer(image_bytes, dtype=np.uint8)
        img = img.reshape((texture.getYSize(), texture.getXSize(), 4))
        img = img[::-1]  # Flip vertically
        img = img[..., :-1]  # Discard useless alpha channel
        img = img[..., ::-1]  # Correct the colors

        return img

    def show_lane_coordinates(self, lanes):
        if self.lane_coordinates_debug_node is not None:
            self.lane_coordinates_debug_node.detachNode()
            self.lane_coordinates_debug_node.removeNode()

        self.lane_coordinates_debug_node = NodePath("Lane Coordinates debug")
        for lane in lanes:
            long_start = lateral_start = lane.position(0, 0)
            lateral_end = lane.position(0, 2)

            long_end = long_start + lane.heading_at(0) * 4
            np_y = self.add_line(Vec3(*long_start, 0), Vec3(*long_end, 0), color=[0, 1, 0, 1], thickness=2)
            np_x = self.add_line(Vec3(*lateral_start, 0), Vec3(*lateral_end, 0), color=[1, 0, 0, 1], thickness=2)
            np_x.reparentTo(self.lane_coordinates_debug_node)
            np_y.reparentTo(self.lane_coordinates_debug_node)
        self.lane_coordinates_debug_node.reparentTo(self.worldNP)

    def remove_show_lane_coordinates(self):
        if self.lane_coordinates_debug_node is not None:
            self.lane_coordinates_debug_node.detachNode()
            self.lane_coordinates_debug_node.removeNode()

    def warmup(self):
        """
        This function automatically initialize models/objects. It can prevent the lagging when creating some objects
        for the first time.
        """
        if self.global_config["preload_models"]:
            from metadrive.component.traffic_participants.pedestrian import Pedestrian
            from metadrive.component.traffic_light.base_traffic_light import BaseTrafficLight
            from metadrive.component.static_object.traffic_object import TrafficBarrier
            from metadrive.component.static_object.traffic_object import TrafficCone
            Pedestrian.init_pedestrian_model()
            warm_up_pedestrian = self.spawn_object(Pedestrian, position=[0, 0], heading_theta=0, record=False)
            warm_up_light = self.spawn_object(BaseTrafficLight, lane=None, position=[0, 0], record=False)
            barrier = self.spawn_object(TrafficBarrier, position=[0, 0], heading_theta=0, record=False)
            cone = self.spawn_object(TrafficCone, position=[0, 0], heading_theta=0, record=False)
            for vel in Pedestrian.SPEED_LIST:
                warm_up_pedestrian.set_velocity([1, 0], vel - 0.1)
                self.taskMgr.step()
            self.clear_objects([warm_up_pedestrian.id, warm_up_light.id, barrier.id, cone.id], record=False)
            warm_up_pedestrian = None
            warm_up_light = None
            barrier = None
            cone = None
