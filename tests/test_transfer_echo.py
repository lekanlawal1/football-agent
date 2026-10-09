"""Summer-transfer echoes: the API re-files a finished season under the player's next club."""
import pandas as pd

from src.api_football_transform import echo_suspects


def rows(*items):
    cols = ["player_id", "player", "team", "team_id", "competition", "season", "minutes", "goals"]
    return pd.DataFrame([dict(zip(cols, r)) for r in items])


def test_echo_is_suspected():
    df = rows((1, "Cunha", "Wolves", 39, "Premier League", "2024/2025", 2603, 15),
              (1, "Cunha", "Manchester United", 33, "Premier League", "2024/2025", 2600, 15))
    assert len(echo_suspects(df)) == 1


def test_real_mid_season_move_is_kept():
    df = rows((2, "Rashford", "Manchester United", 33, "Premier League", "2024/2025", 983, 4),
              (2, "Rashford", "Aston Villa", 66, "Premier League", "2024/2025", 441, 2))
    assert echo_suspects(df).empty
