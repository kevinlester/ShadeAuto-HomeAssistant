"""Exercise the actual coordinator with a minimal HA framework substitute.

No Home Assistant installation or shade hardware is needed for these command tests.
"""
import ast
import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List
import unittest

ROOT = Path(os.environ.get('SHADE_SOURCE', Path(__file__).resolve().parents[1] / 'custom_components' / 'shadeauto'))


class CoordinatorBase:
    @classmethod
    def __class_getitem__(cls, item):
        return cls


def load_coordinator():
    namespace = dict(globals(), DataUpdateCoordinator=CoordinatorBase,
                     HomeAssistant=object, ShadeAutoApi=object, UpdateFailed=RuntimeError)
    # Strip imports only; all production constants and methods execute unchanged.
    for name in ('const.py', 'coordinator.py'):
        tree = ast.parse((ROOT / name).read_text())
        tree.body = [n for n in tree.body if (not isinstance(n, (ast.Import, ast.ImportFrom))
                     or (isinstance(n, ast.ImportFrom) and n.module == '__future__'))
                     and not (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
                              and n.target.id == 'PLATFORMS')]
        exec(compile(tree, str(ROOT / name), 'exec'), namespace)
    return namespace


MODULE = load_coordinator()
Coordinator, Motion = MODULE['ShadeAutoCoordinator'], MODULE['ShadeMotionState']

class EntityBase:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

class Features:
    OPEN, CLOSE, SET_POSITION = 1, 2, 4

MODULE.update(CoordinatorEntity=EntityBase, CoverEntity=type('CoverEntity', (), {}),
              CoverEntityFeature=Features)
cover_tree = ast.parse((ROOT / 'cover.py').read_text())
cover_tree.body = [n for n in cover_tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))
                   or (isinstance(n, ast.ImportFrom) and n.module == '__future__')]
exec(compile(cover_tree, str(ROOT / 'cover.py'), 'exec'), MODULE)
Cover = MODULE['ShadeAutoCover']


class FakeApi:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def control(self, uid, **targets):
        await asyncio.sleep(0)  # force the concurrent caller to run during the POST
        if self.fail:
            raise RuntimeError('hub unavailable')
        self.calls.append((uid, targets))


def make_coordinator():
    coord = object.__new__(Coordinator)
    coord.api = FakeApi()
    coord._rail_command_locks = {}
    coord._motion = {}
    coord._last_cmd = {}
    coord._peripherals = {'1': {'module_type': 33}, '2': {'module_type': 32}}
    coord.data = {'status': {'1': {'BottomRailPosition': 0, 'MiddleRailPosition': 100},
                             '2': {'BottomRailPosition': 0}}}
    coord.config_entry = None
    coord.async_update_listeners = lambda: None
    coord._test_seq = 0
    coord.logger = logging.getLogger('test')
    def register(uid, target, rail='bottom'):
        key = f'{uid}:{rail}'
        coord._test_seq += 1
        cid = coord._test_seq
        coord._last_cmd[key] = {'id': cid, 'target': target}
        coord._motion[key] = Motion(pending_target=target, in_motion=True)
        return cid
    coord.register_command = register  # do not start HA notification/verify tasks
    return coord


class RailCommands(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_moves_preserve_both_targets_in_either_order(self):
        for moves in ([('bottom', 40), ('middle', 70)],
                      [('middle', 70), ('bottom', 40)]):
            coord = make_coordinator()
            await asyncio.gather(*(coord.async_move_rail('1', rail, target)
                                   for rail, target in moves))
            self.assertEqual(coord.api.calls[-1], ('1', {'bottom': 40, 'middle': 70}))

    async def test_full_open_from_reported_position_both_orders_and_schedules(self):
        for concurrent in (False, True):
            for rails in (('bottom', 'middle'), ('middle', 'bottom')):
                coord = make_coordinator()
                coord.data['status']['1'].update(BottomRailPosition=35, MiddleRailPosition=35)
                targets = [(rail, 100 if not MODULE['RAIL_INVERT'][rail] else 0) for rail in rails]
                if concurrent:
                    await asyncio.gather(*(coord.async_move_rail('1', rail, target)
                                           for rail, target in targets))
                else:
                    for rail, target in targets:
                        await coord.async_move_rail('1', rail, target)
                self.assertEqual(coord.api.calls[-1][1], {'bottom': 100, 'middle': 100})
                for uid, payload in coord.api.calls:
                    self.assertLessEqual(payload['bottom'], payload['middle'])

    async def test_raising_bottom_makes_room_and_registers_companion(self):
        coord = make_coordinator()
        coord.data['status']['1'].update(BottomRailPosition=0, MiddleRailPosition=35)
        await coord.async_move_rail('1', 'bottom', 100)
        self.assertEqual(coord.api.calls[-1][1], {'bottom': 100, 'middle': 100})
        self.assertEqual(coord._motion['1:middle'].pending_target, 100)

    async def test_lowering_top_makes_room(self):
        coord = make_coordinator()
        coord.data['status']['1'].update(BottomRailPosition=100, MiddleRailPosition=100)
        await coord.async_move_rail('1', 'middle', 20)
        self.assertEqual(coord.api.calls[-1][1], {'bottom': 20, 'middle': 20})

    async def test_gap_at_travel_limits(self):
        for rail, target, expected in [('bottom', 100, 95), ('middle', 0, 5)]:
            coord = make_coordinator()
            coord._collision_config = lambda: (True, 5)
            got = await coord.async_move_rail('1', rail, target)
            self.assertEqual(got, expected)
            payload = coord.api.calls[-1][1]
            self.assertGreaterEqual(payload['middle'] - payload['bottom'], 5)

    async def test_missing_companion_fails_without_sending_partial_payload(self):
        coord = make_coordinator()
        del coord.data['status']['1']['MiddleRailPosition']
        with self.assertRaises(RuntimeError):
            await coord.async_move_rail('1', 'bottom', 100)
        self.assertEqual(coord.api.calls, [])

    async def test_disabled_guard_preserves_companion(self):
        coord = make_coordinator()
        coord._collision_config = lambda: (False, 0)
        coord.data['status']['1'].update(MiddleRailPosition=35)
        await coord.async_move_rail('1', 'bottom', 100)
        self.assertEqual(coord.api.calls[-1][1], {'bottom': 100, 'middle': 35})

    async def test_failed_post_does_not_register_a_phantom_target(self):
        coord = make_coordinator()
        coord.api.fail = True
        with self.assertRaises(RuntimeError):
            await coord.async_move_rail('1', 'bottom', 40)
        self.assertEqual(coord._last_cmd, {})
        self.assertEqual(coord._motion, {})

    async def test_single_rail_payload_is_unchanged(self):
        coord = make_coordinator()
        await coord.async_move_rail('2', 'bottom', 100)
        self.assertEqual(coord.api.calls, [('2', {'bottom': 100})])

    async def test_retry_preserves_sibling_pending_target(self):
        coord = make_coordinator()
        await coord.async_move_rail('1', 'bottom', 40)
        await coord.async_move_rail('1', 'middle', 70)
        await coord._async_retry_rail('1', 'bottom', 40, 1)
        self.assertEqual(coord.api.calls[-1], ('1', {'bottom': 40, 'middle': 70}))

    async def test_retry_checks_supersession_after_acquiring_lock(self):
        coord = make_coordinator()
        await coord.async_move_rail('1', 'bottom', 40)
        lock = coord._rail_command_locks['1']
        async with lock:
            retry = asyncio.create_task(coord._async_retry_rail('1', 'bottom', 40, 1))
            await asyncio.sleep(0)
            coord.register_command('1', 20, rail='bottom')
        await retry
        self.assertEqual(len(coord.api.calls), 1)


class PositionDisplay(unittest.TestCase):
    def make_cover(self, rail):
        cover = object.__new__(Cover)
        cover._uid = '1'
        cover._rail = rail
        cover._rail_field = MODULE['RAIL_FIELDS'][rail]
        cover._invert = MODULE['RAIL_INVERT'][rail]
        cover.coordinator = make_coordinator()
        cover.coordinator.data['status']['1'].update(BottomRailPosition=35, MiddleRailPosition=35)
        cover._estimation_config = lambda: (True, 28)
        cover._est_start_pos = 35
        cover._est_target = 0
        cover._est_start_ts = time.monotonic() - 100
        cover._est_travel_time_s = 28
        return cover

    def test_expired_estimate_displays_reported_rail_height(self):
        for rail in ('bottom', 'middle'):
            cover = self.make_cover(rail)
            self.assertEqual(cover.current_cover_position, 35)
            self.assertFalse(cover.is_closed)

    def test_new_move_estimates_from_measurement_not_pending_destination(self):
        cover = self.make_cover('bottom')
        cover._est_start_pos = None
        cover.coordinator._motion['1:bottom'] = Motion(in_motion=True, pending_target=100)
        cover._start_estimation_for_command(100)
        self.assertEqual(cover._est_start_pos, 35)
        self.assertAlmostEqual(cover._est_travel_time_s, 18.2)

    def test_new_move_discards_an_expired_estimate(self):
        cover = self.make_cover('bottom')
        cover._start_estimation_for_command(100)
        self.assertEqual(cover._est_start_pos, 35)


if __name__ == '__main__':
    unittest.main()
