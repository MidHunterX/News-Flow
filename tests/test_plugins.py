"""Offline tests for the plugins browser (app/routes/plugins.py).

Points ``PLUGINS_DIR`` at a throwaway tree, so the real ``plugins/`` folder
is never read and no network is involved. The route handlers are called
directly (like the other route tests): rendering happens when the
``TemplateResponse`` is built, so the template is exercised too.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import HTTPException
from starlette.responses import FileResponse, RedirectResponse

from app.routes import plugins as plugin_routes


def _url_for(name: str, **params: str) -> str:
    """The app's Starlette ``url_for``, minus the routing (static only)."""
    if name != "static":
        raise AssertionError(f"tests only stub the static route, got {name!r}")
    return "/static/" + params["path"]


@pytest.fixture
def plugins_dir(tmp_path, monkeypatch):
    """A throwaway plugins tree plus the template's ``url_for`` stub."""
    root = tmp_path / "plugins"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "helper.js").write_text("// helper\n", encoding="utf-8")
    (root / "script.jsx").write_text("// layout\napp.doStuff();\n", encoding="utf-8")
    (root / "logo.bin").write_bytes(b"\x89PNG\x00\x00binary")
    monkeypatch.setattr(plugin_routes, "PLUGINS_DIR", root)
    monkeypatch.setitem(plugin_routes.templates.env.globals, "url_for", _url_for)
    return root


def _html(response) -> str:
    return response.body.decode("utf-8")


# ---------------------------------------------------------------------------
# Directory listing
# ---------------------------------------------------------------------------


async def test_root_listing_puts_folders_first(plugins_dir):
    response = await plugin_routes.plugins_view(request=object(), path=None)

    assert [entry["name"] for entry in response.context["entries"]] == [
        "nested",
        "logo.bin",
        "script.jsx",
    ]
    assert response.context["current_path"] == ""
    assert response.context["parent_href"] is None
    # Folders navigate, files open their preview.
    assert response.context["entries"][0]["href"] == "/plugins?path=nested"
    assert (
        "/plugins/view?path=script.jsx" in _html(response)
    )


async def test_subfolder_listing_has_breadcrumbs(plugins_dir):
    response = await plugin_routes.plugins_view(request=object(), path="nested")

    assert [entry["name"] for entry in response.context["entries"]] == ["helper.js"]
    assert [crumb["label"] for crumb in response.context["crumbs"]] == [
        "plugins",
        "nested",
    ]
    assert response.context["parent_href"] == "/plugins"


async def test_file_path_redirects_to_preview(plugins_dir):
    response = await plugin_routes.plugins_view(request=object(), path="script.jsx")

    assert isinstance(response, RedirectResponse)
    assert response.headers["location"] == "/plugins/view?path=script.jsx"


async def test_empty_folder_renders_an_explanation(plugins_dir):
    (plugins_dir / "empty").mkdir()

    response = await plugin_routes.plugins_view(request=object(), path="empty")

    assert response.context["entries"] == []
    assert "This folder is empty." in _html(response)


async def test_missing_path_is_404(plugins_dir):
    with pytest.raises(HTTPException) as err:
        await plugin_routes.plugins_view(request=object(), path="nope")

    assert err.value.status_code == 404


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../secrets.txt",  # up and out
        "nested/../../secrets.txt",  # still out after resolving
        "/etc/passwd",  # absolute paths are made relative, then miss
    ],
)
async def test_paths_outside_the_plugins_dir_are_refused(plugins_dir, path):
    with pytest.raises(HTTPException) as err:
        await plugin_routes.plugins_view(request=object(), path=path)

    assert err.value.status_code == 404


async def test_symlink_escaping_the_plugins_dir_is_refused(plugins_dir, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (plugins_dir / "link.txt").symlink_to(outside)

    with pytest.raises(HTTPException) as err:
        await plugin_routes.plugin_file_view(request=object(), path="link.txt")

    assert err.value.status_code == 404


# ---------------------------------------------------------------------------
# File preview
# ---------------------------------------------------------------------------


async def test_text_file_preview_shows_content(plugins_dir):
    response = await plugin_routes.plugin_file_view(
        request=object(), path="script.jsx"
    )

    file = response.context["file"]
    assert file["content"] == "// layout\napp.doStuff();\n"
    assert file["note"] is None
    assert [crumb["label"] for crumb in response.context["crumbs"]] == [
        "plugins",
        "script.jsx",
    ]
    assert response.context["download_href"] == "/plugins/download?path=script.jsx"


async def test_binary_file_is_not_inlined(plugins_dir):
    response = await plugin_routes.plugin_file_view(request=object(), path="logo.bin")

    file = response.context["file"]
    assert file["content"] is None
    assert file["note"] == "Binary file — download it instead."


async def test_oversized_file_is_not_inlined(plugins_dir, monkeypatch):
    monkeypatch.setattr(plugin_routes, "MAX_PREVIEW_BYTES", 4)

    response = await plugin_routes.plugin_file_view(
        request=object(), path="script.jsx"
    )

    file = response.context["file"]
    assert file["content"] is None
    assert "Too large" in file["note"]


async def test_folder_cannot_be_previewed(plugins_dir):
    with pytest.raises(HTTPException) as err:
        await plugin_routes.plugin_file_view(request=object(), path="nested")

    assert err.value.status_code == 404


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


async def test_download_file_is_an_attachment(plugins_dir):
    response = await plugin_routes.plugin_download(path="nested/helper.js")

    assert isinstance(response, FileResponse)
    assert response.filename == "helper.js"
    assert response.headers["content-disposition"] == 'attachment; filename="helper.js"'


async def test_download_folder_is_a_zip(plugins_dir):
    response = await plugin_routes.plugin_download(path=None)

    assert response.media_type == "application/zip"
    assert 'filename="plugins.zip"' in response.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(response.body))
    assert sorted(archive.namelist()) == [
        "plugins/logo.bin",
        "plugins/nested/helper.js",
        "plugins/script.jsx",
    ]


async def test_download_subfolder_keeps_its_name(plugins_dir):
    response = await plugin_routes.plugin_download(path="nested")

    assert 'filename="nested.zip"' in response.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(response.body))
    assert archive.namelist() == ["nested/helper.js"]


async def test_download_missing_path_is_404(plugins_dir):
    with pytest.raises(HTTPException) as err:
        await plugin_routes.plugin_download(path="nope")

    assert err.value.status_code == 404


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def test_base_template_has_a_hamburger_menu_linking_to_plugins(plugins_dir):
    """Every page carries the hamburger with its Plugins entry."""
    html = plugin_routes.templates.env.get_template("base.html").render(
        toggles={}
    )

    assert 'id="hamburger-button"' in html
    assert 'id="hamburger-menu"' in html
    assert 'href="/plugins"' in html
    assert "toggleHamburger()" in html


async def test_plugins_page_highlights_the_menu_entry(plugins_dir):
    response = await plugin_routes.plugins_view(request=object(), path=None)

    assert response.context["active_nav"] == "plugins"
