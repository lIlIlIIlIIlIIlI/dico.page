import base64
import unittest
from threading import Barrier
from unittest.mock import patch

import httpx
from api.index import app
from api.routes.module import media_storage as media


class MediaStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_status_accepts_the_plural_routes_used_by_the_picker(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        checked_keys = []

        async def writable(kind, item_id):
            return {"images": [], "files": []}, None

        class Uploads:
            def find_one(self, query):
                checked_keys.append(query["_id"])
                return None

        client = app.test_client()
        with patch.object(media, "_write_target", writable), patch.object(media, "collection", return_value=Uploads()):
            for route in ("images", "videos", "files"):
                response = await client.get("/api/media/posts/34/" + route + "/" + media_id + "/status")
                self.assertEqual(response.status_code, 200, route)
                self.assertFalse((await response.get_json())["complete"])

        self.assertEqual(checked_keys, [
            "posts:34:" + media_id,
            "posts:34:" + media_id,
            "file:posts:34:" + media_id,
        ])

    async def test_outdated_upload_script_receives_reload_instruction(self):
        media_id = "11111111-1111-4111-8111-111111111111"

        async def writable(kind, item_id):
            return {"images": [], "files": []}, None

        client = app.test_client()
        with patch.object(media, "_write_target", writable):
            response = await client.post(
                "/api/media/posts/1/videos/" + media_id
                + "/chunks/0?name=clip.mp4&mime=video%2Fmp4&size=8&count=1&chunk_size=4194304",
                data=b"\0\0\0\x18ftyp", headers={"Content-Type": "video/mp4"},
            )
        self.assertEqual(response.status_code, 409)
        payload = await response.get_json()
        self.assertTrue(payload["reload_required"])
        self.assertIn("새로고침", payload["message"])

    async def test_video_poster_is_saved_served_and_removed_with_draft(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        poster = b"RIFF" + b"\0" * 4 + b"WEBP" + b"preview"

        async def writable(kind, item_id):
            return {"images": []}, None

        async def ready():
            return None

        class Uploads:
            def find_one(self, query):
                return {"user_id": 1, "uuid": media_id, "name": "clip.mp4", "mime_type": "video/mp4",
                        "total_size": 3, "chunk_count": 1, "chunk_size": media.VIDEO_CHUNK_BYTES,
                        "uploaded_chunks": [0], "sha256": None,
                        "chunks": {"0": {"index": 0, "sha": "a" * 40, "size": 3, "sha256": "c" * 64}}}

            def delete_one(self, query):
                return None

            def update_one(self, query, update):
                return None

        class UploadParts:
            def delete_many(self, query):
                return None

        client = app.test_client()
        async with client.session_transaction() as session:
            session["user_id"] = 1
        url = "/api/media/posts/1/videos/" + media_id + "/complete"
        encoded = "data:image/webp;base64," + base64.b64encode(poster).decode()
        with patch.object(media, "_write_target", writable), patch.object(
                media, "collection", side_effect=lambda name: UploadParts() if name == "media_upload_parts" else Uploads()), \
                patch.object(media, "_queued_blob", return_value="b" * 40), \
                patch.object(media, "_commit_blobs") as commit, patch.object(media, "_save_attachment", return_value=True) as save:
            result = await client.post(url, json={"poster": encoded, "sha256": "d" * 64})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(commit.call_args.args[0][-1], ("게시글/1/영상/" + media_id + "/poster.webp", "b" * 40))
            attachment = save.call_args.args[2]
            self.assertEqual(attachment["poster"], {"sha": "b" * 40, "size": len(poster)})
            self.assertIn("게시글/1/영상/" + media_id + "/poster.webp", media._media_paths("posts", 1, attachment))

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value={"images": [attachment]}), patch.object(media, "_read_blob", return_value=poster) as read:
            result = await client.get("/media/posts/1/" + media_id + "?poster=1")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.content_type, "image/webp")
            self.assertEqual(await result.get_data(), poster)
            read.assert_called_once_with("b" * 40)

    def test_github_raw_mode_returns_bytes_without_base64_decoding(self):
        payload = b"\0\0\0\x18ftypbinary-video"
        response = httpx.Response(200, content=payload, request=httpx.Request("GET", "https://api.github.com"))
        with patch.dict("os.environ", {"postimage": "test-token"}), patch.object(media, "_github_client") as client:
            client.return_value.request.return_value = response
            self.assertEqual(media._github("GET", "/git/blobs/" + "f" * 40, raw=True), payload)
            self.assertEqual(client.return_value.request.call_args.kwargs["headers"]["Accept"], "application/vnd.github.raw+json")

    def test_github_rate_limit_returns_retry_delay(self):
        response = httpx.Response(403, json={"message": "secondary rate limit"},
                                  headers={"retry-after": "45"},
                                  request=httpx.Request("POST", "https://api.github.com"))
        with patch.dict("os.environ", {"postimage": "test-token"}), patch.object(media, "_github_client") as client:
            client.return_value.request.return_value = response
            with self.assertRaises(media.MediaRateLimitError) as problem:
                media._github("POST", "/git/blobs", {"content": "x", "encoding": "base64"})
            self.assertEqual(problem.exception.retry_after, 45)

    def test_blob_reads_raw_bytes_and_reuses_warm_cache(self):
        sha = "f" * 40
        media._read_blob.cache_clear()
        with patch.object(media, "_github", return_value=b"video bytes") as github:
            self.assertEqual(media._read_blob(sha), b"video bytes")
            self.assertEqual(media._read_blob(sha), b"video bytes")
            github.assert_called_once_with("GET", "/git/blobs/" + sha, raw=True)
        media._read_blob.cache_clear()

    async def test_video_allows_10_gib_with_4_mib_requests_and_50_mib_github_chunks(self):
        media_id = "11111111-1111-4111-8111-111111111111"

        async def writable(kind, item_id):
            return {"images": []}, None

        class Uploads:
            state = None

            def find_one(self, query, projection=None):
                return self.state

            def update_one(self, query, update, upsert=False):
                if self.state is None:
                    self.state = dict(update["$setOnInsert"])
                return None

        class Parts:
            def replace_one(self, query, document, upsert=False):
                self.document = document

            def find(self, query):
                class Cursor(list):
                    def sort(self, field, direction):
                        return self
                return Cursor()

            def delete_many(self, query):
                return None

        client = app.test_client()
        async with client.session_transaction() as session:
            session["user_id"] = 1
        base = "/api/media/posts/9/videos/" + media_id + "/chunks/0?name=test.mp4&mime=video%2Fmp4&size="
        signature = b"\0\0\0\x18ftyp" + b"\0" * (media.WIRE_CHUNK_BYTES - 8)
        count = (media.MAX_VIDEO_BYTES + media.VIDEO_CHUNK_BYTES - 1) // media.VIDEO_CHUNK_BYTES
        args = "&count=" + str(count) + "&chunk_size=" + str(media.VIDEO_CHUNK_BYTES) + "&chunk_index=0&chunk_offset=0"
        uploads, parts = Uploads(), Parts()
        with patch.object(media, "_write_target", writable), patch.object(
                media, "collection", side_effect=lambda name: parts if name == "media_upload_parts" else uploads), \
                patch.object(media, "_queued_blob", return_value="a" * 40):
            valid = await client.post(base + str(media.MAX_VIDEO_BYTES) + args, data=signature,
                                      headers={"Content-Type": "video/mp4"})
            self.assertEqual(valid.status_code, 200)
            too_large = await client.post(base + str(media.MAX_VIDEO_BYTES + 1) + args, data=signature,
                                          headers={"Content-Type": "video/mp4"})
            self.assertEqual(too_large.status_code, 400)

    async def test_video_group_assembles_unsorted_staged_parts_without_mongo_sort(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        payload = b"\0\0\0\x18ftyp" + b"a" * 8 + b"b" * 16 + b"c" * 8

        async def writable(kind, item_id):
            return {"images": []}, None

        class Uploads:
            state = None

            def find_one(self, query, projection=None):
                return self.state

            def update_one(self, query, changes, upsert=False):
                if self.state is None:
                    self.state = dict(changes.get("$setOnInsert", {}))
                for key, value in changes.get("$set", {}).items():
                    if key.startswith("chunks."):
                        self.state.setdefault("chunks", {})[key.split(".", 1)[1]] = value
                    else:
                        self.state[key] = value
                value = changes.get("$addToSet", {}).get("uploaded_chunks")
                if value is not None and value not in self.state["uploaded_chunks"]:
                    self.state["uploaded_chunks"].append(value)

        class Parts:
            documents = {}

            def replace_one(self, query, document, upsert=False):
                self.documents[document["_id"]] = document

            def find(self, query):
                return list(reversed([part for part in self.documents.values()
                                      if part["upload_id"] == query["upload_id"]
                                      and part["logical_index"] == query["logical_index"]]))

            def delete_many(self, query):
                self.documents = {key: part for key, part in self.documents.items()
                                  if part["upload_id"] != query["upload_id"]
                                  or part["logical_index"] != query["logical_index"]}

        client = app.test_client()
        async with client.session_transaction() as session:
            session["user_id"] = 1
        uploads, staged = Uploads(), Parts()
        base = "/api/media/posts/9/videos/" + media_id
        args = "?name=test.mp4&mime=video%2Fmp4&size=40&count=1&chunk_size=40&chunk_index=0&chunk_offset="
        with patch.object(media, "_write_target", writable), patch.object(
                media, "collection", side_effect=lambda name: staged if name == "media_upload_parts" else uploads), \
                patch.object(media, "WIRE_CHUNK_BYTES", 16), patch.object(media, "VIDEO_CHUNK_BYTES", 40), \
                patch.object(media, "_queued_blob", return_value="a" * 40) as write:
            for index, offset in enumerate((0, 16, 32)):
                response = await client.post(base + "/chunks/" + str(index) + args + str(offset),
                                             data=payload[offset:offset + 16],
                                             headers={"Content-Type": "video/mp4"})
                self.assertEqual(response.status_code, 200)
            self.assertEqual((await response.get_json())["uploaded_chunks"], [0])
            write.assert_called_once_with(payload)

    async def test_general_file_limit_remains_100_mib(self):
        media_id = "11111111-1111-4111-8111-111111111111"

        async def writable(kind, item_id):
            return {"files": []}, None

        with patch.object(media, "_write_target", writable), patch.object(media, "_blob") as write:
            response = await app.test_client().post(
                "/api/media/posts/1/files/" + media_id + "/chunks/0?name=report.pdf&mime=application%2Foctet-stream&size="
                + str(100 * 1024 * 1024 + 1) + "&count=3&chunk_size="
                + str(media.VIDEO_CHUNK_BYTES) + "&chunk_index=0&chunk_offset=0",
                data=b"%PDF-", headers={"Content-Type": "application/octet-stream"},
            )
            self.assertEqual(response.status_code, 400)
            write.assert_not_called()

    async def test_file_upload_commits_into_separate_attachment_folder(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        payload = b"%PDF-sample"

        class Uploads:
            state = None

            def find_one(self, query, projection=None):
                return self.state

            def update_one(self, query, changes, upsert=False):
                if self.state is None:
                    self.state = dict(changes["$setOnInsert"])
                if "$set" in changes:
                    self.state.update(changes["$set"])
                for path, value in changes.get("$set", {}).items():
                    if path.startswith("chunks."):
                        self.state.setdefault("chunks", {})[path.split(".", 1)[1]] = value
                value = changes.get("$addToSet", {}).get("uploaded_chunks")
                if value is not None and value not in self.state.setdefault("uploaded_chunks", []):
                    self.state["uploaded_chunks"].append(value)

            def delete_one(self, query):
                self.state = None

        class Parts:
            document = None

            def replace_one(self, query, document, upsert=False):
                self.document = document

            def find(self, query):
                class Cursor(list):
                    def sort(self, field, direction):
                        return self
                return Cursor([self.document])

            def delete_many(self, query):
                self.document = None

        async def writable(kind, item_id):
            return {"files": []}, None

        uploads = Uploads()
        client = app.test_client()
        async with client.session_transaction() as session:
            session["user_id"] = 1

        base = "/api/media/posts/1/files/" + media_id
        args = "?name=sample.pdf&mime=application%2Foctet-stream&size=" + str(len(payload)) + "&count=1&chunk_size=" + str(media.VIDEO_CHUNK_BYTES) + "&chunk_index=0&chunk_offset=0"
        upload_parts = Parts()
        with patch.object(media, "_write_target", writable), patch.object(
                media, "collection", side_effect=lambda name: upload_parts if name == "media_upload_parts" else uploads), \
                patch.object(media, "_queued_blob", return_value="a" * 40), \
                patch.object(media, "_commit_blobs") as commit, patch.object(media, "_save_attachment", return_value=True) as save:
            part = await client.post(base + "/chunks/0" + args, data=payload, headers={"Content-Type": "application/octet-stream"})
            self.assertEqual(part.status_code, 200)
            complete = await client.post(base + "/complete", json={"sha256": "e" * 64})
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(commit.call_args.args[0][0][0], "게시글/1/첨부파일/" + media_id + "/0000.part")
            self.assertEqual(save.call_args.args[3], "files")

    async def test_image_upload_stages_small_requests_and_persists_completed_metadata(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        image = b"\x89PNG\r\n\x1a\n"

        class Uploads:
            state = None

            def find_one(self, query, projection=None):
                return self.state

            def update_one(self, query, changes, upsert=False):
                if set(changes.get("$setOnInsert", {})) & set(changes.get("$set", {})):
                    raise AssertionError("MongoDB update operators target the same field")
                if self.state is None:
                    self.state = dict(changes.get("$setOnInsert", {}))
                for path, value in changes.get("$set", {}).items():
                    if path.startswith("chunks."):
                        self.state.setdefault("chunks", {})[path.split(".", 1)[1]] = value
                    else:
                        self.state[path] = value
                value = changes.get("$addToSet", {}).get("uploaded_chunks")
                if value is not None and value not in self.state.setdefault("uploaded_chunks", []):
                    self.state["uploaded_chunks"].append(value)

            def delete_one(self, query):
                self.state = None

        class Parts:
            documents = []

            def replace_one(self, query, document, upsert=False):
                self.documents = [part for part in self.documents if part["_id"] != document["_id"]]
                self.documents.append(document)

            def find(self, query):
                class Cursor(list):
                    def sort(self, field, direction):
                        return Cursor(sorted(self, key=lambda part: part[field]))
                return Cursor([part for part in self.documents
                               if part["upload_id"] == query["upload_id"]
                               and part["logical_index"] == query["logical_index"]])

            def delete_many(self, query):
                self.documents = [part for part in self.documents if part["upload_id"] != query["upload_id"]]

        async def writable(kind, item_id):
            return {"images": [], "files": []}, None

        uploads, parts = Uploads(), Parts()
        client = app.test_client()
        async with client.session_transaction() as session:
            session["user_id"] = 1
        collection = lambda name: parts if name == "media_upload_parts" else uploads
        base = "/api/media/posts/8/images/" + media_id
        args = ("?name=picture.png&mime=image%2Fpng&size=8&count=1&chunk_size="
                + str(media.IMAGE_CHUNK_BYTES) + "&chunk_index=0&chunk_offset=0")
        with patch.object(media, "_write_target", writable), patch.object(media, "collection", side_effect=collection), \
                patch.object(media, "_queued_blob", return_value="a" * 40), \
                patch.object(media, "_commit_blobs") as commit, patch.object(media, "_save_attachment", return_value=True) as save:
            part = await client.post(base + "/chunks/0" + args, data=image, headers={"Content-Type": "image/png"})
            self.assertEqual(part.status_code, 200)
            status = await part.get_json()
            self.assertEqual(status["uploaded_chunks"], [0])
            self.assertEqual(uploads.state["uuid"], media_id)
            self.assertEqual(uploads.state["mime_type"], "image/png")
            self.assertEqual(uploads.state["total_size"], len(image))
            self.assertEqual(uploads.state["chunk_count"], 1)
            self.assertEqual(uploads.state["chunk_size"], media.IMAGE_CHUNK_BYTES)
            self.assertEqual(uploads.state["sha256"], None)
            self.assertIn("expire_at", uploads.state)

            complete = await client.post(base + "/complete", json={"sha256": "b" * 64})
            self.assertEqual(complete.status_code, 200)
            self.assertEqual(commit.call_args.args[0], [("게시글/8/이미지/" + media_id + "/0000.part", "a" * 40)])
            saved = save.call_args.args[2]
            self.assertEqual(saved["sha256"], "b" * 64)
            self.assertEqual(saved["uuid"], media_id)
            self.assertEqual(saved["uploaded_chunks"], [0])

    async def test_chunked_image_is_reassembled_for_preview(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        image = b"\x89PNG\r\n\x1a\n"
        document = {"images": [{
            "id": media_id, "kind": "image", "mime": "image/png", "size": len(image),
            "chunks": [{"sha": "a" * 40, "size": 4}, {"sha": "b" * 40, "size": 4}],
        }]}

        async def ready():
            return None

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value=document), \
                patch.object(media, "_read_blob", side_effect=[image[:4], image[4:]]) as read:
            response = await app.test_client().get("/media/posts/1/" + media_id)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(await response.get_data(), image)
            self.assertEqual(read.call_count, 2)

    async def test_file_upload_rejects_extension_with_invalid_signature(self):
        media_id = "11111111-1111-4111-8111-111111111111"

        async def writable(kind, item_id):
            return {"images": [], "files": []}, None

        with patch.object(media, "_write_target", writable), patch.object(media, "_blob") as write:
            response = await app.test_client().post(
                "/api/media/posts/1/files/" + media_id + "/chunks/0?name=report.pdf&mime=application%2Foctet-stream&size=5&count=1&chunk_size="
                + str(media.VIDEO_CHUNK_BYTES) + "&chunk_index=0&chunk_offset=0",
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

    async def test_new_video_seeks_across_four_mib_chunk_boundary(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        size = media.VIDEO_CHUNK_BYTES
        document = {"images": [{"id": media_id, "kind": "video", "mime": "video/mp4",
                                "size": size + 3, "chunk_size": size,
                                "chunks": [{"sha": "a" * 40, "size": size},
                                           {"sha": "b" * 40, "size": 3}]}]}

        async def ready():
            return None

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value=document), patch.object(media, "_read_blob", side_effect=lambda sha: b"a" * size if sha == "a" * 40 else b"xyz"):
            response = await app.test_client().get("/media/posts/1/" + media_id,
                                                   headers={"Range": "bytes=" + str(size - 2) + "-" + str(size + 2)})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(await response.get_data(), b"aaxyz")

    async def test_video_range_streams_beyond_three_mib_with_bounded_concurrency(self):
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
            if 1 <= shas.index(sha) <= 4:
                barrier.wait(timeout=3)
            return parts[shas.index(sha)]

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value=document), patch.object(media, "_read_blob", side_effect=read) as reader:
            response = await app.test_client().get("/media/posts/1/" + media_id,
                                                   headers={"Range": "bytes=0-" + str(6 * media.CHUNK_BYTES - 1)})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(await response.get_data(), b"".join(parts))
            self.assertEqual(response.headers["Content-Range"], "bytes 0-" + str(6 * media.CHUNK_BYTES - 1) + "/" + str(6 * media.CHUNK_BYTES))
            self.assertEqual(response.headers["Content-Length"], str(6 * media.CHUNK_BYTES))
            self.assertEqual(reader.call_count, 6)

    async def test_video_stream_yields_first_chunk_before_reading_the_next(self):
        media_id = "11111111-1111-4111-8111-111111111111"
        document = {"images": [{
            "id": media_id, "kind": "video", "mime": "video/mp4",
            "size": 2 * media.CHUNK_BYTES,
            "chunks": [{"sha": "a" * 40, "size": media.CHUNK_BYTES},
                       {"sha": "b" * 40, "size": media.CHUNK_BYTES}],
        }]}

        async def ready():
            return None

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value=document), patch.object(media, "_read_blob", side_effect=[b"a" * media.CHUNK_BYTES, b"b" * media.CHUNK_BYTES]) as reader:
            async with app.test_request_context("/media/posts/1/" + media_id, headers={"Range": "bytes=0-"}):
                response = await media.serve_media("posts", 1, media_id)
                stream = response.response.__aiter__()
                self.assertEqual(await stream.__anext__(), b"a" * media.CHUNK_BYTES)
                self.assertEqual(reader.call_count, 1)
                self.assertEqual(await stream.__anext__(), b"b" * media.CHUNK_BYTES)
                self.assertEqual(reader.call_count, 2)

    async def test_open_video_range_is_bounded_and_followup_range_can_continue(self):
        self.assertEqual(media.OPEN_PLAYBACK_RANGE_BYTES, 10 * 1024 * 1024)
        media_id = "11111111-1111-4111-8111-111111111111"
        size = 201 * 1024 * 1024
        document = {"images": [{
            "id": media_id, "kind": "video", "mime": "video/mp4", "size": size,
            "chunks": [{"sha": "a" * 40, "size": media.CHUNK_BYTES} for _ in range(size // media.CHUNK_BYTES)],
        }]}

        async def ready():
            return None

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value=document), patch.object(media, "_read_blob", return_value=b"a" * media.CHUNK_BYTES) as reader:
            async with app.test_request_context("/media/posts/1/" + media_id, headers={"Range": "bytes=0-"}):
                response = await media.serve_media("posts", 1, media_id)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.headers["Content-Range"], "bytes 0-" + str(media.OPEN_PLAYBACK_RANGE_BYTES - 1) + "/" + str(size))
                self.assertEqual(response.headers["Content-Length"], str(media.OPEN_PLAYBACK_RANGE_BYTES))
                reader.assert_called_once()
            async with app.test_request_context("/media/posts/1/" + media_id,
                                                headers={"Range": "bytes=" + str(media.OPEN_PLAYBACK_RANGE_BYTES) + "-"}):
                continuation = await media.serve_media("posts", 1, media_id)
                self.assertEqual(continuation.status_code, 206)
                self.assertEqual(continuation.headers["Content-Range"],
                                 "bytes " + str(media.OPEN_PLAYBACK_RANGE_BYTES) + "-" +
                                 str(2 * media.OPEN_PLAYBACK_RANGE_BYTES - 1) + "/" + str(size))

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
