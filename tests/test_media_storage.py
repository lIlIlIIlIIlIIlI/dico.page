import unittest
from unittest.mock import patch

from api.index import app
from api.routes.module import media_storage as media


class MediaStorageTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_missing_attachment_returns_404_without_fetching_github(self):
        async def ready():
            return None

        with patch.object(media, "ensure_database", ready), patch.object(media, "_document", return_value={"images": []}), patch.object(media, "_read_blob") as read:
            response = await app.test_client().get("/media/posts/1/11111111-1111-4111-8111-111111111111")
            self.assertEqual(response.status_code, 404)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
