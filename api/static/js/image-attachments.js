window.DicoImageAttachments = window.DicoImageAttachments || (() => {
    const wireChunkSize = 4 * 1024 * 1024;
    const imageChunkSize = 5 * 1024 * 1024;
    const videoChunkSize = 50 * 1024 * 1024;
    const maxVideo = 10 * 1024 ** 3;
    const maxOriginalImage = 8 * 1024 * 1024;
    const imageTypes = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
    const videoTypes = new Set(['video/mp4', 'video/webm', 'video/ogg']);

    function videoPoster(entry) {
        const video = document.createElement('video');
        const canvas = document.createElement('canvas');
        if (!video.canPlayType || !canvas.toBlob || !window.FileReader) return Promise.resolve(null);
        return new Promise(resolve => {
            let settled = false;
            let capturing = false;
            const finish = value => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                video.removeAttribute('src');
                video.load();
                resolve(value);
            };
            const capture = () => {
                if (settled || capturing || !video.videoWidth || !video.videoHeight) return;
                capturing = true;
                try {
                    const scale = Math.min(1, 640 / Math.max(video.videoWidth, video.videoHeight));
                    canvas.width = Math.round(video.videoWidth * scale);
                    canvas.height = Math.round(video.videoHeight * scale);
                    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
                    canvas.toBlob(blob => {
                        if (!blob || blob.type !== 'image/webp' || blob.size > 128 * 1024) return finish(null);
                        const reader = new FileReader();
                        reader.onload = () => finish(reader.result);
                        reader.onerror = () => finish(null);
                        reader.readAsDataURL(blob);
                    }, 'image/webp', 0.65);
                } catch (_) { finish(null); }
            };
            const timer = setTimeout(() => finish(null), 8000);
            video.muted = true;
            video.preload = 'auto';
            video.playsInline = true;
            video.addEventListener('loadedmetadata', () => {
                if (Number.isFinite(video.duration) && video.duration > 0) {
                    try { video.currentTime = Math.min(0.5, video.duration / 4); } catch (_) { capture(); }
                }
            }, {once: true});
            video.addEventListener('loadeddata', () => { if (!video.seeking) capture(); }, {once: true});
            video.addEventListener('seeked', capture, {once: true});
            video.addEventListener('error', () => finish(null), {once: true});
            video.src = entry.previewUrl;
            try { video.load(); } catch (_) { finish(null); }
        });
    }

    async function send(url, body, mime, csrfToken, signal, onRateLimit, onProgress) {
        for (let attempt = 1; attempt <= 8; attempt++) {
            if (signal?.aborted) throw new Error('업로드가 취소되었습니다.');
            let response;
            let data = {};
            try {
                const options = {
                    method: 'POST', body, credentials: 'same-origin',
                    headers: {'Content-Type': mime, 'X-CSRF-Token': csrfToken, 'Accept': 'application/json'},
                    signal
                };
                response = window.DicoMediaUpload
                    ? await window.DicoMediaUpload.post(url, body, options, onProgress)
                    : await fetch(url, options);
                try { data = await response.json(); } catch (_) { data = {}; }
            } catch (error) {
                if (signal?.aborted) throw new Error('업로드가 취소되었습니다.');
                if (attempt === 8) throw error;
                const seconds = Math.min(60, 2 ** (attempt - 1)) + Math.random() * 0.5;
                onRateLimit?.(seconds);
                await wait(seconds, signal);
                continue;
            }
            if (response.ok && data.success) return data;
            const retryable = response.status === 403 || response.status === 429 || response.status >= 500;
            if (retryable && attempt < 8) {
                const backoff = Math.min(60, 2 ** (attempt - 1)) + Math.random() * 0.5;
                const retryAfter = retryAfterSeconds(response, data);
                const seconds = Math.min(3600, Math.max(backoff, retryAfter || 0));
                onRateLimit?.(seconds);
                await wait(seconds, signal);
                continue;
            }
            throw new Error(data.message || (response.status === 413
                ? '파일 크기가 서버 요청 한도를 초과했습니다.'
                : '파일을 저장하지 못했습니다. 다시 시도해 주세요.'));
        }
    }

    function retryAfterSeconds(response, data) {
        const bodyDelay = Number(data.retry_after);
        const header = response.headers?.get?.('Retry-After');
        let headerDelay = 0;
        if (header) {
            const value = Number(header);
            if (Number.isFinite(value)) headerDelay = Math.max(0, value);
            else {
                const date = Date.parse(header);
                if (Number.isFinite(date)) headerDelay = Math.max(0, (date - Date.now()) / 1000);
            }
        }
        return Math.max(Number.isFinite(bodyDelay) ? bodyDelay : 0, headerDelay);
    }

    function wait(seconds, signal) {
        return new Promise((resolve, reject) => {
            if (signal?.aborted) { reject(new Error('업로드가 취소되었습니다.')); return; }
            const cancel = () => { clearTimeout(timer); reject(new Error('업로드가 취소되었습니다.')); };
            const timer = setTimeout(() => {
                signal?.removeEventListener('abort', cancel);
                resolve();
            }, seconds * 1000);
            signal?.addEventListener('abort', cancel, {once: true});
        });
    }

    function mount(form, options = {}) {
        const root = form.querySelector('[data-image-attachments]');
        if (!root) return null;
        const picker = root.querySelector('[data-image-picker]');
        const zone = root.querySelector('[data-image-dropzone]');
        const list = root.querySelector('[data-image-list]');
        const count = root.querySelector('[data-image-count]');
        const status = root.querySelector('[data-image-status]');
        const menu = root.querySelector('[data-image-menu]');
        const contentInput = options.contentInput;
        let files = [];
        let pending = Promise.resolve();
        const activeUploads = new Set();
        const pool = window.DicoUploadQueue.createPool(1);
        let locked = false;
        let stopped = false;
        let targetId = null;
        let statusCheck = null;
        const csrfToken = form.querySelector('[name="csrf_token"]').value;

        function uploadManifestKey(draftId) {
            return 'dico-media-uploads:' + options.kind + ':' + draftId;
        }

        function readManifest(draftId) {
            try {
                const value = JSON.parse(window.sessionStorage?.getItem(uploadManifestKey(draftId)) || '[]');
                return Array.isArray(value) ? value : [];
            }
            catch (_) { return []; }
        }

        function writeManifest(draftId, records) {
            try {
                const key = uploadManifestKey(draftId);
                if (records.length) window.sessionStorage?.setItem(key, JSON.stringify(records));
                else window.sessionStorage?.removeItem(key);
            } catch (_) {}
        }

        function fingerprint(file, name) {
            return [name, file.size, file.lastModified || 0, file.type || ''].join('\u0000');
        }

        function rememberEntry(entry, draftId) {
            if (!draftId) return;
            const records = readManifest(draftId).filter(record => record.id !== entry.id);
            records.push({id: entry.id, fingerprint: entry.fingerprint});
            writeManifest(draftId, records);
        }

        function forgetEntry(entry, draftId) {
            if (!draftId) return;
            writeManifest(draftId, readManifest(draftId).filter(record => record.id !== entry.id));
        }

        function track(task) {
            activeUploads.add(task);
            task.finally(() => activeUploads.delete(task)).catch(() => {});
            return task;
        }

        async function ready() {
            await pending;
            await Promise.all(Array.from(activeUploads));
        }

        function announce(message, error = false) {
            status.textContent = message;
            status.classList.toggle('text-error', error);
        }

        function changed() {
            contentInput?.dispatchEvent(new Event('input', {bubbles: true}));
        }

        function videoForToken(line) {
            return files.find(entry => !entry.removed && videoTypes.has(entry.file.type)
                && (line.trim() === '#[' + entry.id + ']' || line.trim() === '#[' + entry.name + ']'));
        }

        function markerFor(entry) {
            return '#[' + entry.id + ']';
        }

        function mediaForToken(line) {
            const candidate = line.trim();
            return files.find(entry => !entry.removed && (candidate === markerFor(entry)
                || (imageTypes.has(entry.file.type)
                    && candidate === '![' + entry.name + '](dico-image:' + entry.id + ')')));
        }

        function mediaForPreviewHeading(text) {
            const match = /^\[DICO-IMAGE-([0-9a-f-]{36})\]$/.exec(text);
            if (match) return files.find(entry => !entry.removed && imageTypes.has(entry.file.type) && entry.id === match[1]);
            return mediaForToken('#' + text);
        }

        function previewText() {
            let fence = null;
            return contentInput.value.replace(/\r\n?/g, '\n').split('\n').map(line => {
                const trimmed = line.trim();
                if (!fence && /^(`{3,}|~{3,})(.*?)\1$/.test(trimmed)) return line;
                const marker = trimmed.match(/^(`{3,}|~{3,})/);
                if (marker) {
                    fence = fence === marker[1] ? null : marker[1];
                    return line;
                }
                const entry = !fence && mediaForToken(line);
                return entry && imageTypes.has(entry.file.type) ? '#[DICO-IMAGE-' + entry.id + ']' : line;
            }).join('\n');
        }

        function insertMedia(entry) {
            if (!contentInput) return;
            const marker = markerFor(entry);
            let text = contentInput.value;
            let start = contentInput.selectionStart;
            let end = contentInput.selectionEnd;
            let offset = 0;
            const locations = [];
            for (const line of text.split('\n')) {
                if (line.trim() === marker) locations.push([offset, offset + line.length]);
                offset += line.length + 1;
            }
            for (const [from, to] of locations.reverse()) {
                text = text.slice(0, from) + text.slice(to);
                start = start > to ? start - (to - from) : Math.min(start, from);
                end = end > to ? end - (to - from) : Math.min(end, from);
            }
            const before = text.slice(0, start);
            const after = text.slice(end);
            const insert = (before && !before.endsWith('\n\n') ? before.endsWith('\n') ? '\n' : '\n\n' : '')
                + marker + (after.startsWith('\n\n') ? '' : after.startsWith('\n') ? '\n' : '\n\n');
            if (text.length - (end - start) + insert.length > contentInput.maxLength) {
                announce('본문 글자 수 제한을 초과했습니다.', true);
                return;
            }
            contentInput.value = text;
            contentInput.setRangeText(insert, start, end, 'end');
            changed();
            contentInput.focus();
            announce(entry.name + ' 위치를 본문에 표시했습니다. 원하는 줄로 옮길 수 있습니다.');
        }

        function render() {
            count.textContent = files.length + '/5';
            list.replaceChildren();
            files.forEach((entry) => {
                const item = document.createElement('li');
                item.className = 'flex min-w-0 flex-wrap items-center gap-2 rounded-lg border border-base-200 bg-base-200/30 p-2 sm:flex-nowrap';
                if (imageTypes.has(entry.file.type) || entry.posterData) {
                    const thumb = document.createElement('img');
                    thumb.src = entry.posterData || entry.previewUrl;
                    thumb.alt = '';
                    thumb.className = 'size-10 shrink-0 rounded object-cover';
                    item.appendChild(thumb);
                } else {
                    const icon = document.createElement('span');
                    icon.className = 'flex size-10 shrink-0 items-center justify-center rounded bg-primary/10 text-primary';
                    icon.textContent = '▶';
                    item.appendChild(icon);
                }
                const label = document.createElement('span');
                label.className = 'min-w-0 flex-1 text-sm';
                const filename = document.createElement('span');
                filename.className = 'block truncate';
                filename.textContent = entry.name;
                filename.title = entry.name;
                label.appendChild(filename);
                const state = document.createElement('span');
                state.className = 'shrink-0 text-xs text-base-content/55';
                state.textContent = entry.removed ? '삭제 중' : entry.failed ? '업로드 실패' : entry.uploading ? entry.progress || '업로드 중' : '준비 중';
                const remove = document.createElement('button');
                remove.type = 'button';
                remove.className = 'btn btn-ghost btn-xs shrink-0 text-error';
                remove.textContent = '삭제';
                remove.disabled = locked;
                remove.addEventListener('click', () => {
                    if (locked || entry.removed) return;
                    entry.removed = true;
                    entry.controller.abort();
                    render();
                    pending = pending.then(async () => {
                        if (entry.task) await entry.task;
                        if (options.kind && targetId) {
                            await send('/api/media/drafts/' + options.kind + '/' + targetId + '/remove/' + entry.id,
                                '{}', 'application/json', csrfToken);
                            forgetEntry(entry, targetId);
                        }
                        URL.revokeObjectURL(entry.previewUrl);
                        files = files.filter(item => item !== entry);
                        if (contentInput) {
                            const marker = markerFor(entry);
                            contentInput.value = contentInput.value.split('\n')
                                .filter(line => line.trim() !== marker).join('\n');
                        }
                        render();
                        changed();
                        announce(entry.name + ' 파일을 삭제했습니다.');
                    }).catch(error => {
                        entry.removed = false;
                        render();
                        announce(error.message, true);
                    });
                });
                item.appendChild(label);
                if (!entry.uploaded || entry.removed) item.appendChild(state);
                if (contentInput) {
                    const insert = document.createElement('button');
                    insert.type = 'button';
                    insert.className = 'btn btn-ghost btn-xs shrink-0 text-primary';
                    insert.textContent = '본문에 삽입';
                    insert.disabled = locked || entry.removed;
                    insert.addEventListener('click', () => insertMedia(entry));
                    item.appendChild(insert);
                }
                item.appendChild(remove);
                list.appendChild(item);
            });
        }

        async function prepare(file) {
            if (videoTypes.has(file.type)) {
                if (!file.size || file.size > maxVideo) throw new Error((file.name || '영상') + ': 영상은 10GB 이하로 올려 주세요.');
                return file;
            }
            if (!imageTypes.has(file.type)) throw new Error((file.name || '이미지') + ': PNG, JPG, WebP, GIF 또는 MP4, WebM, OGG만 지원합니다.');
            if (!file.size || file.size > maxOriginalImage) throw new Error((file.name || '이미지') + ': 원본 이미지는 8MB 이하로 선택해 주세요.');
            return file;
        }

        function addFiles(selected) {
            if (locked || stopped) return ready();
            pending = pending.then(async () => {
                for (const source of Array.from(selected)) {
                    if (stopped) break;
                    if (files.length >= 5) { announce('첨부는 최대 5개까지 가능합니다.', true); break; }
                    try {
                        const file = await prepare(source);
                        if (stopped) break;
                        const name = typeof source.name === 'string' ? source.name.slice(0, 120) : '붙여넣은 이미지';
                        if (options.ensureDraft) targetId = await options.ensureDraft();
                        const fileFingerprint = fingerprint(source, name || '붙여넣은 이미지');
                        const manifest = targetId ? readManifest(targetId) : [];
                        const resumed = manifest.find(record => record.fingerprint === fileFingerprint
                            && !files.some(entry => entry.id === record.id && !entry.removed));
                        files.push({
                            id: resumed?.id || crypto.randomUUID(), name: name || '붙여넣은 이미지',
                            file, previewUrl: URL.createObjectURL(file), controller: new AbortController(),
                            completedChunks: new Set(), uploaded: false,
                            failed: false, removed: false, uploading: false,
                            fingerprint: fileFingerprint, resumed: Boolean(resumed)
                        });
                        const entry = files[files.length - 1];
                        render();
                        insertMedia(entry);
                        if (videoTypes.has(file.type)) {
                            entry.posterPromise = videoPoster(entry).then(data => {
                                if (!entry.removed && !stopped && data) {
                                    entry.posterData = data;
                                    render();
                                    changed();
                                }
                                return data;
                            }).catch(() => null);
                        }
                        if (options.ensureDraft) {
                            entry.task = track(pool.add(async () => {
                                if (stopped || entry.removed) return;
                                try {
                                    await preflight();
                                    targetId = await options.ensureDraft();
                                    if (!stopped && !entry.removed) {
                                        rememberEntry(entry, targetId);
                                        await uploadEntry(entry, options.kind, targetId, csrfToken);
                                        if (!entry.removed) announce('');
                                    }
                                } catch (error) {
                                    if (!entry.removed) {
                                        entry.failed = true;
                                        render();
                                        announce(error.message, true);
                                    }
                                }
                            }));
                        } else {
                            announce(files.length + '개 파일이 준비되었습니다.');
                        }
                    } catch (error) { announce(error.message, true); }
                }
            });
            return ready();
        }

        async function preflight() {
            if (!files.length) return;
            if (!statusCheck) {
                statusCheck = fetch('/api/media/status', {credentials: 'same-origin'}).then(async response => {
                    const data = await response.json();
                    if (!response.ok || !data.configured) {
                        throw new Error(data.message || '서버에 postimage GitHub 토큰이 설정되지 않았습니다.');
                    }
                }).catch(error => { statusCheck = null; throw error; });
            }
            return statusCheck;
        }

        async function uploadEntry(entry, kind, id, token) {
            const base = '/api/media/' + kind + '/' + id;
            entry.uploading = true;
            entry.failed = false;
            render();
            try {
                const mime = entry.file.type;
                const name = encodeURIComponent(entry.name);
                const image = imageTypes.has(mime);
                const mediaRoute = image ? 'images' : 'videos';
                const logicalSize = image ? imageChunkSize : videoChunkSize;
                const total = Math.ceil(entry.file.size / logicalSize);
                const statusResponse = await fetch(base + '/' + mediaRoute + '/' + entry.id + '/status', {
                    credentials: 'same-origin', headers: {'X-CSRF-Token': token, 'Accept': 'application/json'},
                    signal: entry.controller.signal
                });
                let serverState = {};
                try { serverState = await statusResponse.json(); } catch (_) {}
                if (!statusResponse.ok || !serverState.success) {
                    throw new Error(serverState.message || '저장된 업로드 상태를 확인하지 못했습니다.');
                }
                if (serverState.complete) {
                    entry.completedChunks = new Set(Array.from({length: total}, (_, index) => index));
                    entry.uploaded = true;
                    return;
                }
                if (serverState.total_size && serverState.total_size !== entry.file.size
                    || serverState.mime_type && serverState.mime_type !== mime
                    || serverState.chunk_size && serverState.chunk_size !== logicalSize) {
                    throw new Error('이어올릴 파일의 이름 또는 내용이 기존 업로드와 다릅니다.');
                }
                entry.completedChunks = new Set(serverState.uploaded_chunks || []);
                const direct = serverState.protocol_version === 2;
                const chunksPerLogical = Math.ceil(logicalSize / wireChunkSize);
                const digest = window.DicoSha256.create();
                let bytesRead = 0;
                let confirmed = direct ? Number(serverState.uploaded_bytes || 0) : 0;
                const progress = (acknowledged, inFlight = 0, label = '업로드 중') => {
                    const value = Math.min(99.9, (acknowledged + inFlight) / entry.file.size * 100);
                    entry.displayPercent = Math.max(entry.displayPercent || 0, value);
                    entry.progress = label + ' · ' + entry.displayPercent.toFixed(2) + '%';
                    render();
                };
                if (direct) {
                    if (!Number.isSafeInteger(confirmed) || confirmed < 0 || confirmed > entry.file.size) {
                        throw new Error('이어올릴 업로드 위치가 올바르지 않습니다.');
                    }
                    for (let offset = 0; offset < confirmed; offset += wireChunkSize) {
                        digest.update(new Uint8Array(await entry.file.slice(offset, Math.min(confirmed, offset + wireChunkSize)).arrayBuffer()));
                    }
                    progress(confirmed);
                    let cursor = confirmed;
                    while (cursor < entry.file.size) {
                        if (stopped || entry.removed) throw new Error('업로드가 취소되었습니다.');
                        const logicalIndex = Math.floor(cursor / logicalSize);
                        const chunkOffset = cursor - logicalIndex * logicalSize;
                        const end = Math.min(entry.file.size, (logicalIndex + 1) * logicalSize,
                            cursor + (window.DicoMediaUpload?.chunkSize(entry.uploadRate) || wireChunkSize));
                        const body = new Uint8Array(await entry.file.slice(cursor, end).arrayBuffer());
                        const args = '?protocol=direct-v2&name=' + name + '&mime=' + encodeURIComponent(mime)
                            + '&size=' + entry.file.size + '&count=' + total + '&chunk_size=' + logicalSize
                            + '&chunk_index=' + logicalIndex + '&chunk_offset=' + chunkOffset
                            + '&wire_size=' + (window.DicoMediaUpload?.chunkSize(entry.uploadRate) || wireChunkSize);
                        const started = Date.now();
                        let measured = false;
                        const responseData = await send(base + '/' + mediaRoute + '/' + entry.id + '/chunks/' + cursor + args,
                            body, mime, token, entry.controller.signal,
                            seconds => { progress(cursor, 0, '재시도 ' + Math.ceil(seconds) + '초 후'); },
                            (loaded, length) => {
                                progress(cursor, Math.min(loaded, body.length), '전송 중');
                                const elapsed = (Date.now() - started) / 1000;
                                if (elapsed > 0.1 && loaded > 128 * 1024) {
                                    const rate = loaded / elapsed;
                                    entry.uploadRate = measured ? entry.uploadRate * 0.6 + rate * 0.4 : rate;
                                    measured = true;
                                }
                                if (loaded >= length) progress(cursor, body.length, 'GitHub 저장 중');
                            });
                        if (responseData.uploaded_bytes !== end) {
                            throw new Error('저장된 업로드 위치가 일치하지 않습니다. 새로고침 후 다시 시도해 주세요.');
                        }
                        digest.update(body);
                        cursor = confirmed = end;
                        progress(confirmed);
                    }
                } else for (let logicalIndex = 0; logicalIndex < total; logicalIndex++) {
                    const groupStart = logicalIndex * logicalSize;
                    const groupSize = Math.min(logicalSize, entry.file.size - groupStart);
                    for (let chunkOffset = 0; chunkOffset < groupSize; chunkOffset += wireChunkSize) {
                        if (stopped || entry.removed) throw new Error('업로드가 취소되었습니다.');
                        const start = groupStart + chunkOffset;
                        const end = Math.min(groupStart + groupSize, start + wireChunkSize);
                        const body = new Uint8Array(await entry.file.slice(start, end).arrayBuffer());
                        digest.update(body);
                        bytesRead += body.length;
                        if (entry.completedChunks.has(logicalIndex)) { confirmed = bytesRead; progress(confirmed); continue; }
                        const physicalIndex = logicalIndex * chunksPerLogical + chunkOffset / wireChunkSize;
                        const args = '?name=' + name + '&mime=' + encodeURIComponent(mime)
                            + '&size=' + entry.file.size + '&count=' + total + '&chunk_size=' + logicalSize
                            + '&chunk_index=' + logicalIndex + '&chunk_offset=' + chunkOffset;
                        let responseData;
                        do {
                            responseData = await send(base + '/' + mediaRoute + '/' + entry.id + '/chunks/' + physicalIndex + args,
                                body, mime, token, entry.controller.signal, seconds => {
                                    progress(confirmed, 0, '재시도 ' + Math.ceil(seconds) + '초 후');
                                }, loaded => progress(confirmed, Math.min(loaded, body.length), '전송 중'));
                            if (responseData.pending) {
                                entry.progress = 'GitHub에 파일 조각 저장 중';
                                render();
                                await wait(Math.max(1.5, Number(responseData.retry_after) || 0), entry.controller.signal);
                            }
                        } while (responseData.pending);
                        entry.completedChunks = new Set(responseData.uploaded_chunks || entry.completedChunks);
                        confirmed = bytesRead;
                        progress(confirmed);
                    }
                    progress(confirmed);
                }
                if (!entry.removed) {
                    progress(confirmed, 0, 'GitHub에 게시 중');
                    const poster = image ? null : await entry.posterPromise;
                    await send(base + '/' + mediaRoute + '/' + entry.id + '/complete',
                        JSON.stringify({poster, sha256: digest.digest()}), 'application/json', token, entry.controller.signal,
                        seconds => { entry.progress = 'GitHub 요청 제한 · ' + Math.ceil(seconds) + '초 후 재시도'; render(); });
                }
                if (!entry.removed) entry.uploaded = true;
            } finally {
                entry.uploading = false;
                render();
            }
        }

        async function uploadAll(kind, id, token) {
            locked = true;
            picker.disabled = true;
            render();
            await track(window.DicoUploadQueue.run(files.filter(entry => !entry.uploaded && !entry.removed), 1,
                entry => uploadEntry(entry, kind, id, token)));
            announce('첨부파일을 저장했습니다.');
        }

        root.querySelector('[data-image-select]').addEventListener('click', () => {
            menu.open = false;
            picker.click();
        });
        root.querySelector('[data-image-paste]').addEventListener('click', async () => {
            menu.open = false;
            if (navigator.clipboard?.read) {
                try {
                    const entries = await navigator.clipboard.read();
                    const images = [];
                    for (const entry of entries) {
                        const type = entry.types.find(value => imageTypes.has(value));
                        if (type) images.push(await entry.getType(type));
                    }
                    if (images.length) { addFiles(images); return; }
                } catch (_) {}
            }
            zone.focus();
            announce('복사한 이미지를 Ctrl+V로 붙여넣어 주세요.');
        });
        picker.addEventListener('change', () => {
            const selected = Array.from(picker.files);
            picker.value = '';
            addFiles(selected);
        });
        zone.addEventListener('click', () => { if (!locked) picker.click(); });
        form.querySelector('[data-image-open]')?.addEventListener('click', () => {
            if (locked) return;
            menu.open = true;
            root.scrollIntoView({block: 'nearest', behavior: 'smooth'});
        });
        zone.addEventListener('keydown', event => {
            if (!locked && (event.key === 'Enter' || event.key === ' ')) {
                event.preventDefault();
                picker.click();
            }
        });
        zone.addEventListener('dragover', event => {
            if (locked) return;
            event.preventDefault();
            zone.classList.add('border-primary', 'bg-primary/5');
        });
        zone.addEventListener('dragleave', () => zone.classList.remove('border-primary', 'bg-primary/5'));
        zone.addEventListener('drop', event => {
            event.preventDefault();
            zone.classList.remove('border-primary', 'bg-primary/5');
            addFiles(event.dataTransfer.files);
        });
        form.addEventListener('paste', event => {
            if (locked) return;
            const images = Array.from(event.clipboardData?.items || [])
                .filter(item => item.kind === 'file' && imageTypes.has(item.type))
                .map(item => item.getAsFile()).filter(Boolean);
            if (images.length) { event.preventDefault(); addFiles(images); }
        });

        return {
            ready,
            videoForToken,
            mediaForToken,
            mediaForPreviewHeading,
            previewText,
            videos: () => files.filter(entry => !entry.removed && videoTypes.has(entry.file.type)),
            hasFiles: () => files.length > 0,
            stop: () => {
                stopped = true;
                files.forEach(entry => {
                    entry.removed = true;
                    entry.controller.abort();
                });
            },
            preflight,
            uploadAll,
            freeze: () => { locked = true; picker.disabled = true; render(); },
            unfreeze: () => { locked = false; picker.disabled = false; render(); },
            clear: () => {
                files.forEach(entry => URL.revokeObjectURL(entry.previewUrl));
                files = [];
                locked = false;
                picker.disabled = false;
                targetId = null;
                statusCheck = null;
                pending = Promise.resolve();
                stopped = false;
                render();
                announce('작성 칸에서 Ctrl+V로 복사한 이미지도 추가할 수 있습니다.');
            }
        };
    }

    return {mount};
})();
