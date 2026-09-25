# Media storage

Set the server environment variable `postimage` to a GitHub fine-grained personal access token for the private `dico-page/postimage` repository. Grant this repository **Contents: read and write**. Do not expose this token to the browser or add it to source control.

Posts and notices store only media metadata in MongoDB. The server writes actual media to the private GitHub repository:

- `게시글/{id}/이미지/{media_id}.{extension}`
- `게시글/{id}/영상/{media_id}/{chunk_number}.part`
- `게시글/{id}/첨부파일/{file_id}/{chunk_number}.part`
- `공지/{id}/이미지/{media_id}.{extension}`
- `공지/{id}/영상/{media_id}/{chunk_number}.part`
- `공지/{id}/첨부파일/{file_id}/{chunk_number}.part`

Images are at most 768 KiB after optional browser resizing. Videos and general attachments are at most 20 MiB each, transferred as 768 KiB chunks. General attachments support PDF, ZIP, TXT, CSV, HWP, HWPX, DOCX, XLSX and PPTX, with five slots separate from the five image/video slots. A file is committed once all chunks arrive. Incomplete upload records expire after 24 hours. Completed files are served via `/media/<kind>/<id>/<media_id>`; video responses support byte ranges. The download buttons request 768 KiB ranges and assemble files in the browser to stay under Vercel's per-response size limit; direct downloads stream complete files with attachment headers. Existing data URL images still render. Uploaded HTML5 videos use Plyr 3.8.4 from the official CDN; native controls remain available if the CDN cannot load.

Images and videos start uploading as soon as they are selected, using a temporary post or notice ID. Publishing reuses that ID and its uploaded media. Removing an item or canceling the draft deletes its file paths from the repository's current branch and its temporary database state; expired drafts are cleaned when another draft is started. Git commits still retain previously committed media in history, so canceling does not erase historical blobs from GitHub.

Selecting a video inserts `#[filename.mp4]` on its own line at the editor cursor. Moving that line moves the Plyr player in the published post or notice; the preview shows the selected local video there. Videos without a matching marker keep the existing top placement. The separate general attachment list still appears below the content. The file picker copies its selected files before resetting the input, so asynchronous uploads retain the selection.
The image/video attachment list also offers a Preview button for checking each selected file before publishing.

Git history retains uploaded files even after a notice is deleted. Monitor storage growth and GitHub API usage; use object storage for higher traffic or larger videos.
