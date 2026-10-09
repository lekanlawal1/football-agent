"""Shot geometry used by the xG model (src/statsbomb_ingest.py). StatsBomb's pitch is 120 x 80
yards, attacking towards x = 120, with the goal mouth from y = 36 to y = 44."""

import math

from src.statsbomb_ingest import freeze_frame_features, in_triangle, shot_geometry


def test_penalty_spot_distance_and_angle():
    dist, angle = shot_geometry(108, 40)                       # 12 yards, straight on
    assert dist == 12
    assert math.isclose(angle, 2 * math.atan(4 / 12))          # the 8-yard goal seen from 12 yards


def test_angle_shrinks_out_wide_and_far_away():
    _, central = shot_geometry(108, 40)
    _, wide = shot_geometry(118, 10)
    _, far = shot_geometry(80, 40)
    assert wide < central and far < central


def test_point_in_shooting_triangle():
    tri = (100, 40, 120, 36, 120, 44)                          # shooter to both posts
    assert in_triangle(110, 40, *tri)                          # standing in the way
    assert not in_triangle(110, 30, *tri)                      # well wide of the line


def test_freeze_frame_counts_blockers_and_finds_keeper():
    frame = [
        {"teammate": False, "location": [118, 40], "position": {"name": "Goalkeeper"}},
        {"teammate": False, "location": [110, 40], "position": {"name": "Center Back"}},
        {"teammate": False, "location": [110, 20], "position": {"name": "Left Back"}},   # out of the way
        {"teammate": True, "location": [112, 41], "position": {"name": "Center Forward"}},  # own player
    ]
    blockers, gk = freeze_frame_features(100, 40, frame)
    assert blockers == 2                                       # keeper and centre back
    assert math.isclose(gk, 2)


def test_missing_freeze_frame():
    assert freeze_frame_features(100, 40, None) == (None, None)
