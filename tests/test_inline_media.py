import unittest

from api.index import app
from api.routes.home import _inline_photo_ids, _inline_video_ids, _render_markdown


class InlineVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_id_shortcodes_select_each_video_even_with_the_same_filename(self):
        first = {"id": "11111111-1111-4111-8111-111111111111", "kind": "video", "name": "clip.mp4"}
        second = {"id": "22222222-2222-4222-8222-222222222222", "kind": "video", "name": "clip.mp4"}
        content = "앞의 영상\n\n#[" + first["id"] + "]\n\n뒤의 영상\n\n#[" + second["id"] + "]"
        async with app.test_request_context("/posts/17"):
            rendered = _render_markdown(content, [first, second], "posts", 17)
            first_url = "/media/posts/17/" + first["id"]
            second_url = "/media/posts/17/" + second["id"]
            self.assertLess(rendered.index(first_url), rendered.index("뒤의 영상"))
            self.assertLess(rendered.index("뒤의 영상"), rendered.index(second_url))
            self.assertEqual(_inline_video_ids(content, [first, second]), {first["id"], second["id"]})
            self.assertEqual(rendered.count("<video"), 2)

    async def test_id_shortcode_works_for_notice_and_ignores_unknown_id_and_code_fence(self):
        video = {"id": "11111111-1111-4111-8111-111111111111", "kind": "video", "name": "clip.mp4"}
        content = "#[" + video["id"] + "]\n\n#[22222222-2222-4222-8222-222222222222]"
        async with app.test_request_context("/notices/4"):
            rendered = _render_markdown(content, [video], "notices", 4)
            self.assertIn("/media/notices/4/" + video["id"], rendered)
            self.assertEqual(rendered.count("<video"), 1)
            self.assertEqual(_inline_video_ids(content, [video]), {video["id"]})
            fenced = "```\n#[" + video["id"] + "]\n```"
            self.assertEqual(_inline_video_ids(fenced, [video]), set())
            self.assertNotIn("<video", _render_markdown(fenced, [video], "notices", 4))

    async def test_shortcode_renders_player_at_its_line_and_hides_top_duplicate(self):
        video = {"id": "11111111-1111-4111-8111-111111111111", "kind": "video", "name": "VALORANT 02-16.mp4"}
        content = "앞의 글\n\n#[VALORANT 02-16.mp4]\n\n뒤의 글"
        async with app.test_request_context("/posts/12"):
            rendered = _render_markdown(content, [video], "posts", 12)
            self.assertLess(rendered.index("앞의 글"), rendered.index("<video"))
            self.assertLess(rendered.index("<video"), rendered.index("뒤의 글"))
            self.assertIn("/media/posts/12/" + video["id"], rendered)
            self.assertIn("download=1", rendered)
            self.assertEqual(_inline_video_ids(content, [video]), {video["id"]})
            template = app.jinja_env.get_template("layout/attached_videos.html")
            hidden = await template.render_async(images=[video], kind="posts", item_id=12,
                                                 inline_video_ids={video["id"]})
            self.assertNotIn("<video", hidden)
            visible = await template.render_async(images=[video], kind="posts", item_id=12,
                                                  inline_video_ids=set())
            self.assertIn("<video", visible)

    async def test_unknown_and_code_fenced_markers_do_not_embed(self):
        video = {"id": "11111111-1111-4111-8111-111111111111", "kind": "video", "name": "clip.mp4"}
        content = "```text\n#[clip.mp4]\n```\n\n#[missing.mp4]"
        async with app.test_request_context("/notices/4"):
            rendered = _render_markdown(content, [video], "notices", 4)
            self.assertNotIn("<video", rendered)
            self.assertNotIn("/media/notices/4", rendered)
            self.assertEqual(_inline_video_ids(content, [video]), set())

    async def test_filename_is_escaped_in_player_caption(self):
        video = {"id": "11111111-1111-4111-8111-111111111111", "kind": "video", "name": "x<y&z.mp4"}
        async with app.test_request_context("/posts/12"):
            rendered = _render_markdown("#[x<y&z.mp4]", [video], "posts", 12)
            self.assertIn("x&lt;y&amp;z.mp4", rendered)
            self.assertNotIn("<y&z", rendered)

    async def test_uploaded_video_shows_saved_poster_and_warms_stream(self):
        video = {"id": "11111111-1111-4111-8111-111111111111", "kind": "video",
                 "name": "clip.mp4", "poster": {"sha": "a" * 40, "size": 20}}
        async with app.test_request_context("/posts/12"):
            rendered = _render_markdown("#[clip.mp4]", [video], "posts", 12)
            self.assertIn("/media/posts/12/" + video["id"] + "?poster=1", rendered)
            self.assertIn("data-dico-stream", rendered)
            template = app.jinja_env.get_template("layout/attached_videos.html")
            gallery = await template.render_async(images=[video], kind="posts", item_id=12,
                                                  inline_video_ids=set())
            self.assertIn("?poster=1", gallery)
            self.assertIn("data-dico-stream", gallery)

    async def test_photo_marker_renders_at_its_line_without_duplicate_gallery(self):
        photo = {"id": "11111111-1111-4111-8111-111111111111", "kind": "image", "name": "x<y].jpg"}
        other = {"id": "22222222-2222-4222-8222-222222222222", "kind": "image", "name": "other.png"}
        marker = "![" + photo["name"] + "](dico-image:" + photo["id"] + ")"
        content = "앞의 글\n" + marker + "\n뒤의 글"
        async with app.test_request_context("/posts/12"):
            rendered = _render_markdown(content, [photo, other], "posts", 12)
            self.assertLess(rendered.index("앞의 글"), rendered.index("<img"))
            self.assertLess(rendered.index("<img"), rendered.index("뒤의 글"))
            self.assertIn("/media/posts/12/" + photo["id"], rendered)
            self.assertIn("x&lt;y].jpg", rendered)
            self.assertEqual(_inline_photo_ids(content, [photo, other]), {photo["id"]})
            template = app.jinja_env.get_template("layout/attached_images.html")
            gallery = await template.render_async(images=[photo, other], kind="posts", item_id=12,
                                                  inline_photo_ids={photo["id"]})
            self.assertNotIn(photo["id"], gallery)
            self.assertIn(other["id"], gallery)

    async def test_photo_marker_in_code_block_stays_text(self):
        photo = {"id": "11111111-1111-4111-8111-111111111111", "kind": "image", "name": "picture.png"}
        marker = "![picture.png](dico-image:" + photo["id"] + ")"
        content = "```text\n" + marker + "\n```"
        async with app.test_request_context("/notices/4"):
            self.assertEqual(_inline_photo_ids(content, [photo]), set())
            self.assertNotIn("/media/notices/4/" + photo["id"], _render_markdown(content, [photo], "notices", 4))

    async def test_notice_with_inline_photo_has_no_second_photo_section(self):
        photo = {"id": "11111111-1111-4111-8111-111111111111", "kind": "image", "name": "photo.png"}
        content = "알림\n\n![photo.png](dico-image:" + photo["id"] + ")"
        async with app.test_request_context("/notices/4"):
            notice = {"id": 4, "title": "공지", "content": content, "images": [photo], "files": [],
                      "is_pinned": False, "author_nickname": "관리자", "created_at": "2026-09-25T12:00:00",
                      "inline_video_ids": set(), "inline_photo_ids": _inline_photo_ids(content, [photo]),
                      "content_html": _render_markdown(content, [photo], "notices", 4)}
            page = await app.jinja_env.get_template("notices/detail.html").render_async(notice=notice, current_user=None)
            self.assertEqual(page.count('src="/media/notices/4/' + photo["id"] + '"'), 1)
            self.assertNotIn('aria-label="첨부 사진"', page)


if __name__ == "__main__":
    unittest.main()
