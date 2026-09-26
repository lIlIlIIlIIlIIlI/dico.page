import unittest
from unittest.mock import patch

from api.index import app
from api.routes import home
from api.routes.module import media_storage as media
from api.routes.module.admin import admin


class MediaDraftTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_reserves_a_private_draft_id(self):
        async def ready():
            return None

        async def user():
            return {"id": 9, "role": "user"}

        class Drafts:
            saved = None

            def insert_one(self, document):
                self.saved = document

        drafts = Drafts()
        client = app.test_client()
        async with client.session_transaction() as saved:
            saved["user_id"] = 9
            saved["csrf_token"] = "valid-token"
        with patch.object(media, "ensure_database", ready), patch.object(media, "get_current_user", user), patch.object(media, "_clean_expired_drafts_sync"), patch.object(media, "next_id_sync", return_value=12), patch.object(media, "collection", return_value=drafts):
            response = await client.post("/api/media/drafts/posts", headers={"X-CSRF-Token": "valid-token"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual((await response.get_json())["id"], 12)
            self.assertEqual(drafts.saved["_id"], "posts:12")
            self.assertEqual(drafts.saved["author_id"], 9)

    def test_incomplete_upload_removes_unreferenced_github_path(self):
        item = {"id": "11111111-1111-4111-8111-111111111111", "kind": "image", "mime": "image/png"}
        with patch.object(media, "_document", return_value=None), patch.object(media, "_commit_blobs") as commit:
            media._discard_untracked_media("posts", 7, item)
            self.assertEqual(commit.call_args.args[0], [
                ("게시글/7/이미지/" + item["id"] + ".png", None),
            ])

    def test_chunk_sizes_fit_the_function_ingress_limit_and_git_blob_limit(self):
        self.assertEqual(media.WIRE_CHUNK_BYTES, 4 * 1024 * 1024)
        self.assertEqual(media.IMAGE_CHUNK_BYTES, 5 * 1024 * 1024)
        self.assertEqual(media.VIDEO_CHUNK_BYTES, 50 * 1024 * 1024)
        self.assertEqual(media.GITHUB_BLOB_BYTES, 12 * 1024 * 1024)

    def test_large_image_paths_are_saved_as_ordered_chunks(self):
        image_id = "11111111-1111-4111-8111-111111111111"
        paths = media._media_paths("posts", 7, {
            "id": image_id, "kind": "image", "mime": "image/png",
            "chunks": [{"sha": "a" * 40}, {"sha": "b" * 40}],
        })
        self.assertEqual(paths, [
            "게시글/7/이미지/" + image_id + "/0000.part",
            "게시글/7/이미지/" + image_id + "/0001.part",
        ])

    def test_github_blob_writes_are_serialized_with_a_random_gap(self):
        class LockCollection:
            def __init__(self):
                self.state = None

            def update_one(self, query, update, upsert=False):
                if self.state is None:
                    self.state = {"_id": query["_id"], **update["$setOnInsert"]}

            def find_one_and_update(self, query, update):
                now = query["lease_until"]["$lte"]
                if self.state["lease_until"] > now or self.state["next_allowed_at"] > now:
                    return None
                previous = dict(self.state)
                self.state.update(update["$set"])
                return previous

            def find_one(self, query):
                return dict(self.state)

            def update_one_release(self, query, update):
                if self.state.get("lease_id") == query["lease_id"]:
                    self.state.update(update["$set"])
                    self.state.pop("lease_id", None)

            update_one = update_one

        locks = LockCollection()
        original_update = locks.update_one

        def update(query, change, upsert=False):
            if "$unset" in change:
                locks.update_one_release(query, change)
            else:
                original_update(query, change, upsert)

        locks.update_one = update
        with patch.object(media, "collection", return_value=locks), \
                patch.object(media, "_blob", return_value="a" * 40) as blob, \
                patch.object(media.random, "uniform", return_value=1.25):
            self.assertEqual(media._queued_blob(b"part"), "a" * 40)
            with self.assertRaises(media.MediaRateLimitError) as limited:
                media._queued_blob(b"part 2")
        self.assertEqual(blob.call_count, 1)
        self.assertGreaterEqual(limited.exception.retry_after, 1.0)
        self.assertNotIn("lease_id", locks.state)

    async def test_cancel_requires_csrf_and_deletes_only_owned_draft(self):
        async def ready():
            return None

        async def user():
            return {"id": 9, "role": "user"}

        client = app.test_client()
        async with client.session_transaction() as saved:
            saved["user_id"] = 9
            saved["csrf_token"] = "valid-token"

        with patch.object(media, "ensure_database", ready), patch.object(media, "get_current_user", user), patch.object(media, "_remove_draft_sync", return_value=True) as remove:
            url = "/api/media/drafts/posts/7/cancel"
            response = await client.post(url, headers={"X-CSRF-Token": "wrong-token"})
            self.assertEqual(response.status_code, 400)
            remove.assert_not_called()
            response = await client.post(url, headers={"X-CSRF-Token": "valid-token"})
            self.assertEqual(response.status_code, 200)
            remove.assert_called_once_with("posts", 7, 9)

    def test_cancel_removes_committed_image_and_video_paths(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        video_id = "22222222-2222-4222-8222-222222222222"
        draft = {"id": 7, "kind": "posts", "author_id": 9, "draft_state": "active", "files": [], "images": [
            {"id": media_id, "kind": "image", "mime": "image/png"},
            {"id": video_id, "kind": "video", "chunks": [{"sha": "a" * 40}, {"sha": "b" * 40}]},
        ]}

        class Drafts:
            deleted = False

            def find_one(self, query):
                return draft

            def delete_one(self, query):
                self.deleted = True

        class Posts:
            def find_one(self, query, projection):
                return None

        class Uploads:
            removed = False

            def delete_many(self, query):
                self.removed = True

        class UploadParts:
            removed = False

            def delete_many(self, query):
                self.removed = True

        drafts, uploads, upload_parts = Drafts(), Uploads(), UploadParts()
        groups = {"media_drafts": drafts, "posts": Posts(), "media_uploads": uploads,
                  "media_upload_parts": upload_parts}
        with patch.object(media, "collection", side_effect=groups.get), patch.object(media, "_commit_blobs") as commit:
            self.assertTrue(media._remove_draft_sync("posts", 7, 9))
            paths = [path for path, sha in commit.call_args.args[0]]
            self.assertEqual(paths, [
                "게시글/7/이미지/" + media_id + ".png",
                "게시글/7/영상/" + video_id + "/0000.part",
                "게시글/7/영상/" + video_id + "/0001.part",
            ])
            self.assertTrue(all(sha is None for _, sha in commit.call_args.args[0]))
            self.assertTrue(drafts.deleted and uploads.removed and upload_parts.removed)

    def test_publishing_reuses_reserved_ids_and_uploaded_media(self):
        draft = {"id": 7, "images": [{"id": "image"}], "files": [{"id": "file"}]}
        post_records = []
        notice_records = []

        class Posts:
            def insert_one(self, document):
                post_records.append(document)

        class Notices:
            def insert_one(self, document):
                notice_records.append(document)

        with patch.object(home, "claim_media_draft_sync", return_value=draft), patch.object(home, "finish_media_draft_sync"), patch.object(home, "next_id_sync", side_effect=AssertionError("new id")), patch.object(home, "collection", return_value=Posts()):
            self.assertEqual(home._publish_post_draft_sync(7, "제목", "본문", "general", 9, "작성자"), 7)
        with patch.object(admin, "claim_media_draft_sync", return_value=draft), patch.object(admin, "finish_media_draft_sync"), patch("api.routes.module.notifications.next_id_sync", side_effect=AssertionError("new id")), patch("api.routes.module.notifications.collection", return_value=Notices()):
            notice = admin._publish_notice_draft_sync(7, "공지", "본문", False, 9, "관리자")
            self.assertEqual(notice["id"], 7)
        self.assertEqual(post_records[0]["images"], draft["images"])
        self.assertEqual(post_records[0]["files"], draft["files"])
        self.assertEqual(notice_records[0]["images"], draft["images"])

    async def test_another_user_cannot_fetch_draft_media(self):
        media_id = "11111111-1111-4111-8111-111111111111"

        async def ready():
            return None

        draft = {"draft_state": "active", "author_id": 9, "images": [{
            "id": media_id, "name": "private.png", "kind": "image", "size": 8,
            "mime": "image/png", "sha": "a" * 40,
        }]}
        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value=draft), patch.object(media, "_read_blob") as read:
            response = await app.test_client().get("/media/posts/7/" + media_id)
            self.assertEqual(response.status_code, 404)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
