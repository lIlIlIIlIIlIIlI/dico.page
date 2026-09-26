# Media storage

Set the server environment variable `postimage` to a GitHub fine-grained personal access token for the private `dico-page/postimage` repository. Grant this repository **Contents: read and write**. Do not expose this token to the browser or add it to source control.

Posts and notices store only media metadata in MongoDB. The server writes actual media to the private GitHub repository:

- `게시글/{id}/이미지/{media_id}.{extension}`
- `게시글/{id}/영상/{media_id}/{chunk_number}.part`
- `게시글/{id}/영상/{media_id}/poster.webp`
- `게시글/{id}/첨부파일/{file_id}/{chunk_number}.part`
- `공지/{id}/이미지/{media_id}.{extension}`
- `공지/{id}/영상/{media_id}/{chunk_number}.part`
- `공지/{id}/영상/{media_id}/poster.webp`
- `공지/{id}/첨부파일/{file_id}/{chunk_number}.part`

Images are at most 768 KiB after optional browser resizing. Videos are accepted up to 10 GiB in 4 MiB chunks; older videos and general attachments keep their 768 KiB chunks. General attachments remain limited to 100 MiB. Two files upload at a time, with up to three in-flight chunks per file after the first chunk. General attachments support PDF, ZIP, TXT, CSV, HWP, HWPX, DOCX, XLSX and PPTX, with five slots separate from the five image/video slots. A file is committed once all chunks arrive. Incomplete upload records expire after 24 hours. Completed files are served via `/media/<kind>/<id>/<media_id>`; video responses support byte ranges using each video's recorded chunk size. Small downloads use 768 KiB ranges; downloads above 100 MiB use 10 MiB ranges and the File System Access API to write directly to disk without assembling the file in memory. Direct downloads stream complete files with attachment headers. Existing data URL images still render. Uploaded HTML5 videos use Plyr 3.8.4 from the official CDN; native controls remain available if the CDN cannot load.

Playback reads raw GitHub blobs through a shared connection pool. Open-ended video byte-range requests return up to 10 MiB at a time, so the browser can request later ranges during playback instead of downloading the whole video before it starts. Explicit ranges still support up to 200 MiB. The server fetches four adjacent chunks in parallel at a time without buffering the entire response, and warm instances reuse recently read chunks. Existing 768 KiB videos remain readable.

The browser captures a small WebP poster from each new video while uploading and commits it alongside the video chunks. Posts, notices, and editor previews show this frame before playback. A missing poster does not block upload; older videos have no saved poster and fetch metadata when their player approaches the viewport. Draft deletion removes the poster path along with the chunks.

Images and videos start uploading as soon as they are selected, using a temporary post or notice ID. Publishing reuses that ID and its uploaded media. Removing an item or canceling the draft deletes its file paths from the repository's current branch and its temporary database state; expired drafts are cleaned when another draft is started. Git commits still retain previously committed media in history, so canceling does not erase historical blobs from GitHub.

The editor inserts movable photo and video markers (`#[<media_uuid>]`) into the post or notice body. Media with a marker appears at that line and is omitted from the separate photo or video gallery. Older photo markers (`![name](dico-image:<uuid>)`) and video filename markers still render.

Selecting a photo or video inserts `#[<media_uuid>]` on its own line at the editor cursor. Moving that line moves the image or Plyr player in the published post or notice; the preview shows the selected local media there. Once uploaded, the picker displays its direct `/media/<kind>/<id>/<media_uuid>` URL. Older `#[filename.mp4]` markers still render. Media without a matching marker keeps the existing top placement. The separate general attachment list still appears below the content. The file picker copies its selected files before resetting the input, so asynchronous uploads retain the selection.

Git history retains uploaded files even after a notice is deleted. GitHub recommends keeping repositories within 10 GB and limits content-generating API requests, so a 10 GiB video can take hours to upload and may exhaust practical repository capacity; the size setting does not reserve storage or guarantee completion. Rate-limited uploads wait and retry while the editor stays open. Monitor storage growth and GitHub API usage; use object storage for reliable large videos or higher traffic.
