# Media storage

Set the server environment variable `postimage` to a GitHub fine-grained personal access token for the private `dico-page/postimage` repository. Grant this repository **Contents: read and write**. Do not expose this token to the browser or add it to source control.

Posts and notices store only media metadata in MongoDB. The server writes actual media to the private GitHub repository:

- `게시글/{id}/이미지/{media_id}.{extension}`
- `게시글/{id}/영상/{media_id}/{chunk_number}.part`
- `공지/{id}/이미지/{media_id}.{extension}`
- `공지/{id}/영상/{media_id}/{chunk_number}.part`

Images are at most 768 KiB after optional browser resizing. Videos are at most 20 MiB, transferred as 768 KiB chunks. A video is committed once all chunks arrive. Incomplete upload records expire after 24 hours. Completed files are served via `/media/<kind>/<id>/<media_id>`; video responses support byte ranges. Existing data URL images still render.

Git history retains uploaded files even after a notice is deleted. Monitor storage growth and GitHub API usage; use object storage for higher traffic or larger videos.
