from fileconverter.core.output_paths import choose_output_path


def test_uses_plain_name_when_free(tmp_path):
    assert choose_output_path("photo", tmp_path, ".jpg") == tmp_path / "photo.jpg"


def test_adds_number_when_name_is_taken(tmp_path):
    (tmp_path / "photo.jpg").write_bytes(b"existing")
    assert choose_output_path("photo", tmp_path, ".jpg") == tmp_path / "photo (1).jpg"


def test_skips_all_taken_numbers(tmp_path):
    for name in ("photo.jpg", "photo (1).jpg", "photo (2).jpg"):
        (tmp_path / name).write_bytes(b"existing")
    assert choose_output_path("photo", tmp_path, ".jpg") == tmp_path / "photo (3).jpg"


def test_never_returns_the_source_itself(tmp_path):
    # Source already has the target extension (e.g. a PNG mis-named .jpg converted to JPEG).
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"source")
    assert choose_output_path(source.stem, tmp_path, ".jpg") != source
