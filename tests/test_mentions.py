import base64

from arty_cli import mentions, tools

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def test_a_mentioned_file_is_attached_as_text(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    content = mentions.expand("explain @app.py please", tools.Workspace(tmp_path))
    assert content.startswith("explain @app.py please")
    assert "<file path=\"app.py\">\nprint('hi')\n</file>" in content


def test_a_missing_or_outside_file_is_left_alone(tmp_path):
    workspace = tools.Workspace(tmp_path)
    assert mentions.expand("mail me @someone and @../secret.txt", workspace) == "mail me @someone and @../secret.txt"


def test_an_image_is_sent_as_a_picture(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    content = mentions.expand("what's wrong in @shot.png", tools.Workspace(tmp_path))
    assert content[0] == {"type": "text", "text": "what's wrong in @shot.png"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
