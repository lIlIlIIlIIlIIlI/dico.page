import io
import unittest
from threading import Barrier
from unittest.mock import patch

from api.index import app
from api.routes.module import media_storage as media


class MediaStorageTests(unittest.IsolatedAsyncioTestCase):
    def test_github_raw_mode_returns_bytes_without_base64_decoding(self):
        payload = b"\0\0\0\x18ftypbinary-video"
        with patch.dict("os.environ", {"postimage": "test-token"}), patch.object(media, "urlopen", return_value=io.BytesIO(payload)) as open_url:
            self.assertEqual(media._github("GET", "/git/blobs/" + "f" * 40, raw=True), payload)
            request = open_url.call_args.args[0]
            self.assertEqual(request.get_header("Accept"), "application/vnd.github.raw+json")

    def test_blob_reads_raw_bytes_and_reuses_warm_cache(self):
        sha = "f" * 40
        media._read_blob.cache_clear()
        with patch.object(media, "_github", return_value=b"video bytes") as github:
            self.assertEqual(media._read_blob(sha), b"video bytes")
            self.assertEqual(media._read_blob(sha), b"video bytes")
            github.assert_called_once_with("GET", "/git/blobs/" + sha, raw=True)
        media._read_blob.cache_clear()

    async def test_video_allows_100_mib_but_rejects_larger_uploads(self):
        media_id = "11111111-1111-4111-8111-111111111111"

        async def writable(kind, item_id):
            return {"images": []}, None

        class Uploads:
            def find_one(self, query):
                return None

            def update_one(self, query, update, upsert=False):
                return None

        client = app.test_client()
        async with client.session_transaction() as session:
            session["user_id"] = 1
        base = "/api/media/posts/9/videos/" + media_id + "/chunks/0?name=test.mp4&mime=video%2Fmp4&size="
        signature = b"\0\0\0\x18ftyp" + b"\0" * (media.CHUNK_BYTES - 8)
        with patch.object(media, "_write_target", writable), patch.object(media, "collection", return_value=Uploads()), patch.object(media, "_blob", return_value="a" * 40):
            valid = await client.post(base + str(100 * 1024 * 1024) + "&count=134", data=signature,
                                      headers={"Content-Type": "video/mp4"})
            self.assertEqual(valid.status_code, 200)
            too_large = await client.post(base + str(100 * 1024 * 1024 + 1) + "&count=134", data=signature,
                                          headers={"Content-Type": "video/mp4"})
            self.assertEqual(too_large.status_code, 400)

    async def test_file_upload_commits_into_separate_attachment_folder(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        payload = b"%PDF-sample"

        class Uploads:
            state = None

            def find_one(self, query):
                return self.state

            def update_one(self, query, changes, upsert=False):
                if self.state is None:
                    self.state = dict(changes["$setOnInsert"], chunks={})
                self.state["chunks"]["0"] = changes["$set"]["chunks.0"]

            def delete_one(self, query):
                self.state = None

        async def writable(kind, item_id):
            return {"files": []}, None

        uploads = Uploads()
        client = app.test_client()
        async with client.session_transaction() as session:
            session["user_id"] = 1

        base = "/api/media/posts/1/files/" + media_id
        args = "?name=sample.pdf&mime=application%2Foctet-stream&size=" + str(len(payload)) + "&count=1"
        with patch.object(media, "_write_target", writable), patch.object(media, "collection", return_value=uploads), patch.object(media, "_blob", return_value="a" * 40), patch.object(media, "_commit_blobs") as commit, patch.object(media, "_save_attachment", return_value=True) as save:
            part = await client.post(base + "/chunks/0" + args, data=payload, headers={"Content-Type": "application/octet-stream"})
            self.assertEqual(part.status_code, 200)
            complete = await client.post(base + "/complete", json={})
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(commit.call_args.args[0][0][0], "게시글/1/첨부파일/" + media_id + "/0000.part")
            self.assertEqual(save.call_args.args[3], "files")

    async def test_file_upload_rejects_extension_with_invalid_signature(self):
        media_id = "11111111-1111-4111-8111-111111111111"

        async def writable(kind, item_id):
            return {"images": [], "files": []}, None

        with patch.object(media, "_write_target", writable), patch.object(media, "_blob") as write:
            response = await app.test_client().post(
                "/api/media/posts/1/files/" + media_id + "/chunks/0?name=report.pdf&mime=application%2Foctet-stream&size=5&count=1",
                data=b"hello", headers={"Content-Type": "application/octet-stream"},
            )
            self.assertEqual(response.status_code, 400)
            write.assert_not_called()

    async def test_downloaded_file_contains_all_chunks(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        first = b"a" * media.CHUNK_BYTES
        last = b"xyz"
        document = {"files": [{
            "id": media_id, "kind": "file", "name": "자료.pdf",
            "mime": "application/octet-stream", "size": len(first) + len(last),
            "chunks": [{"sha": "a" * 40, "size": len(first)}, {"sha": "b" * 40, "size": len(last)}],
        }]}

        async def ready():
            return None

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value=document), patch.object(media, "_read_blob", side_effect=lambda sha: first if sha == "a" * 40 else last):
            client = app.test_client()
            async with app.test_request_context("/media/posts/1/" + media_id, method="HEAD"):
                head = await media.serve_media("posts", 1, media_id)
                self.assertEqual(head.headers["Content-Length"], str(len(first) + len(last)))
            response = await client.get("/media/posts/1/" + media_id)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(await response.get_data(), first + last)
            self.assertEqual(response.headers["Content-Length"], str(len(first) + len(last)))
            self.assertIn("attachment; filename*=UTF-8''", response.headers["Content-Disposition"])
            self.assertEqual(response.headers["Content-Type"], "application/octet-stream")

    async def test_video_range_returns_requested_chunk_without_joining_entire_video(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        first = b"a" * media.CHUNK_BYTES
        last = b"xyz"
        document = {
            "images": [{
                "id": media_id, "kind": "video", "mime": "video/webm",
                "size": media.CHUNK_BYTES + len(last),
                "chunks": [{"sha": "a" * 40, "size": len(first)}, {"sha": "b" * 40, "size": len(last)}],
            }]
        }

        async def ready():
            return None

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value=document), patch.object(media, "_read_blob", side_effect=lambda sha: first if sha == "a" * 40 else last):
            client = app.test_client()
            response = await client.get(
                "/media/posts/1/" + media_id,
                headers={"Range": "bytes=" + str(media.CHUNK_BYTES) + "-"},
            )
            self.assertEqual(response.status_code, 206)
            self.assertEqual(await response.get_data(), last)
            self.assertEqual(response.headers["Content-Range"], "bytes " + str(media.CHUNK_BYTES) + "-" + str(media.CHUNK_BYTES + 2) + "/" + str(media.CHUNK_BYTES + 3))

    async def test_video_range_reads_four_chunks_concurrently_and_caps_response(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        parts = [bytes([65 + index]) * media.CHUNK_BYTES for index in range(6)]
        shas = [str(index) * 40 for index in range(6)]
        document = {"images": [{
            "id": media_id, "kind": "video", "mime": "video/mp4",
            "size": 6 * media.CHUNK_BYTES,
            "chunks": [{"sha": sha, "size": media.CHUNK_BYTES} for sha in shas],
        }]}

        async def ready():
            return None

        barrier = Barrier(4)

        def read(sha):
            barrier.wait(timeout=3)
            return parts[shas.index(sha)]

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value=document), patch.object(media, "_read_blob", side_effect=read) as reader:
            response = await app.test_client().get("/media/posts/1/" + media_id, headers={"Range": "bytes=0-"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(await response.get_data(), b"".join(parts[:4]))
            self.assertEqual(response.headers["Content-Range"], "bytes 0-" + str(4 * media.CHUNK_BYTES - 1) + "/" + str(6 * media.CHUNK_BYTES))
            self.assertEqual(reader.call_count, 4)

    async def test_video_range_honors_seek_offset_and_explicit_end(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        first = b"a" * media.CHUNK_BYTES
        second = b"b" * media.CHUNK_BYTES
        document = {"images": [{
            "id": media_id, "kind": "video", "mime": "video/mp4",
            "size": len(first) + len(second),
            "chunks": [{"sha": "a" * 40, "size": len(first)}, {"sha": "b" * 40, "size": len(second)}],
        }]}

        async def ready():
            return None

        start = media.CHUNK_BYTES - 2
        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value=document), patch.object(media, "_read_blob", side_effect=[first, second]):
            response = await app.test_client().get("/media/posts/1/" + media_id,
                                                   headers={"Range": "bytes=" + str(start) + "-" + str(start + 4)})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(await response.get_data(), b"aabbb")
            self.assertEqual(response.headers["Content-Range"], "bytes " + str(start) + "-" + str(start + 4) + "/" + str(2 * media.CHUNK_BYTES))

    async def test_missing_attachment_returns_404_without_fetching_github(self):
        async def ready():
            return None

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value={"images": []}), patch.object(media, "_read_blob") as read:
            response = await app.test_client().get("/media/posts/1/11111111-1111-4111-8111-111111111111")
            self.assertEqual(response.status_code, 404)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
