import unittest

from api.index import app
from api.routes.home import _inline_video_ids, _render_markdown


class InlineVideoTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
