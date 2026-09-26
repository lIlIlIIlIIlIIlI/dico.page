import base64
import unittest
from unittest.mock import patch

from api.index import app
from api.routes import home


class PostAuthorProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_and_comments_use_current_avatars_by_author_id(self):
        post = {
            "id": 7, "title": "프로필", "content": "본문", "category": "general",
            "author_id": 10, "author_nickname": "작성자", "views": 3,
            "created_at": "2026-09-25T12:00:00", "images": [], "files": [],
        }
        comments = [
            {"id": 1, "author_id": 11, "author_nickname": "댓글", "content": "첫 댓글", "created_at": "2026-09-25T12:01:00"},
            {"id": 2, "author_id": 12, "author_nickname": "기본", "content": "둘째 댓글", "created_at": "2026-09-25T12:02:00"},
        ]
        avatar = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n123").decode()
        comment_avatar = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n456").decode()
        queried = []

        class Posts:
            def find_one_and_update(self, *args, **kwargs):
                return dict(post)

        class Counts:
            def count_documents(self, *args):
                return 2

            def find_one(self, *args):
                return None

            def find(self, *args):
                return self

            def sort(self, *args):
                return comments

        class Authors:
            def find(self, query, projection):
                queried.append(query["id"]["$in"])
                return [
                    {"id": 10, "user_uid": "author-uid"},
                    {"id": 11, "user_uid": "comment-uid"},
                ]

        class Profiles:
            def find(self, query, projection):
                return [
                    {"user_id": 10, "avatar_url": avatar, "updated_at": "2026-09-25T12:00:00"},
                    {"user_id": 11, "avatar_url": comment_avatar, "updated_at": "2026-09-25T12:01:00"},
                ]

        groups = {"posts": Posts(), "comments": Counts(), "post_likes": Counts(),
                  "users": Authors(), "user_profiles": Profiles()}
        async with app.test_request_context("/posts/7"):
            with patch.object(home, "collection", side_effect=groups.get):
                result, replies, liked = home._get_post_sync(7)

            self.assertEqual(set(queried[0]), {10, 11, 12})
            self.assertEqual(len(queried), 1)
            self.assertTrue(result["avatar_url"].startswith("/avatars/10?v="))
            self.assertEqual(replies[0]["user_uid"], "comment-uid")
            self.assertTrue(replies[0]["avatar_url"].startswith("/avatars/11?v="))
            self.assertFalse(replies[1].get("avatar_url"))
            self.assertFalse(liked)

            result.update(media_kind=None, inline_video_ids=set())
            template = app.jinja_env.get_template("home/detail.html")
            rendered = await template.render_async(post=result, comments=replies, liked=False,
                                                   category_name="자유", current_user=None, csrf_token="test")
        self.assertEqual(rendered.count('src="' + result["avatar_url"].replace("&", "&amp;") + '"'), 2)
        self.assertEqual(rendered.count('src="' + replies[0]["avatar_url"].replace("&", "&amp;") + '"'), 2)
        self.assertNotIn("data:image/", rendered)
        self.assertIn('/profile/author-uid', rendered)
        self.assertIn('/profile/comment-uid', rendered)
        self.assertIn('>기</span>', rendered)

    async def test_new_comment_includes_latest_avatar_for_immediate_display(self):
        saved = []

        class Posts:
            def find_one(self, *args):
                return {"_id": 1}

        class Comments:
            def insert_one(self, document):
                saved.append(document)

        class Authors:
            def find(self, *args):
                return [{"id": 11, "user_uid": "comment-uid"}]

        class Profiles:
            def find(self, *args):
                return [{"user_id": 11, "avatar_url": "data:image/png;base64,YWJj"}]

        groups = {"posts": Posts(), "comments": Comments(), "users": Authors(), "user_profiles": Profiles()}
        async with app.test_request_context("/posts/7/comments"):
            with patch.object(home, "collection", side_effect=groups.get), patch.object(home, "next_id_sync", return_value=4):
                comment = home._create_comment_sync(7, 11, "댓글", "내용")
        self.assertEqual(comment["user_uid"], "comment-uid")
        self.assertEqual(comment["avatar_url"], "/avatars/11?v=0")
        self.assertNotIn("avatar_url", saved[0])

    async def test_avatar_endpoint_reads_saved_image_and_returns_missing_as_404(self):
        avatar = b"\x89PNG\r\n\x1a\n123"

        async def ready():
            pass

        class Profiles:
            def find_one(self, query, projection):
                if query["user_id"] == 10:
                    return {"avatar_url": "data:image/png;base64," + base64.b64encode(avatar).decode()}
                return None

        with patch.object(home, "ensure_database", ready), patch.object(home, "collection", return_value=Profiles()):
            client = app.test_client()
            response = await client.get("/avatars/10")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content_type, "image/png")
            self.assertEqual(await response.get_data(), avatar)
            self.assertEqual((await client.get("/avatars/11")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
