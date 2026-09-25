window.DicoImageAttachments = window.DicoImageAttachments || (() => {
    const chunkSize = 768 * 1024;
    const maxVideo = 20 * 1024 * 1024;
    const maxOriginalImage = 8 * 1024 * 1024;
    const imageTypes = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
    const videoTypes = new Set(['video/mp4', 'video/webm', 'video/ogg']);

    async function send(url, body, mime, csrfToken) {
        const response = await fetch(url, {
            method: 'POST',
            body,
            credentials: 'same-origin',
            headers: {'Content-Type': mime, 'X-CSRF-Token': csrfToken, 'Accept': 'application/json'}
        });
        let data;
        try { data = await response.json(); } catch (_) { data = {}; }
        if (!response.ok || !data.success) {
            throw new Error(data.message || (response.status === 413
                ? '파일 크기가 서버 제한을 초과했습니다.'
                : '파일을 저장하지 못했습니다. 다시 시도해 주세요.'));
        }
        return data;
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
        let locked = false;
        let targetId = null;
        const csrfToken = form.querySelector('[name="csrf_token"]').value;

        function announce(message, error = false) {
            status.textContent = message;
            status.classList.toggle('text-error', error);
        }

        function changed() {
            contentInput?.dispatchEvent(new Event('input', {bubbles: true}));
        }

        function videoForToken(line) {
            return files.find(entry => !entry.removed && videoTypes.has(entry.file.type)
                && line.trim() === '#[' + entry.name + ']');
        }

        function insertVideo(entry) {
            if (!contentInput) return;
            const marker = '#[' + entry.name + ']';
            const start = contentInput.selectionStart;
            const end = contentInput.selectionEnd;
            const before = contentInput.value.slice(0, start);
            const after = contentInput.value.slice(end);
            const insert = (before && !before.endsWith('\n\n') ? before.endsWith('\n') ? '\n' : '\n\n' : '')
                + marker + (after.startsWith('\n\n') ? '' : after.startsWith('\n') ? '\n' : '\n\n');
            if (contentInput.value.length - (end - start) + insert.length > contentInput.maxLength) {
                announce('본문 글자 수 제한을 초과했습니다.', true);
                return;
            }
            contentInput.setRangeText(insert, start, end, 'end');
            changed();
            contentInput.focus();
            announce(entry.name + ' 영상 위치를 본문에 표시했습니다. 원하는 줄로 옮길 수 있습니다.');
        }

        function render() {
            count.textContent = files.length + '/5';
            list.replaceChildren();
            files.forEach((entry) => {
                const item = document.createElement('li');
                item.className = 'flex min-w-0 flex-wrap items-center gap-2 rounded-lg border border-base-200 bg-base-200/30 p-2 sm:flex-nowrap';
                if (imageTypes.has(entry.file.type)) {
                    const thumb = document.createElement('img');
                    thumb.src = entry.previewUrl;
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
                label.className = 'min-w-0 flex-1 truncate text-sm';
                label.textContent = entry.name;
                label.title = entry.name;
                const state = document.createElement('span');
                state.className = 'shrink-0 text-xs text-base-content/55';
                state.textContent = entry.removed ? '삭제 중' : entry.uploaded ? '업로드 완료' : entry.failed ? '업로드 실패' : entry.uploading ? '업로드 중' : '준비 중';
                const remove = document.createElement('button');
                remove.type = 'button';
                remove.className = 'btn btn-ghost btn-xs shrink-0 text-error';
                remove.textContent = '삭제';
                remove.disabled = locked;
                remove.addEventListener('click', () => {
                    if (locked || entry.removed) return;
                    entry.removed = true;
                    render();
                    pending = pending.then(async () => {
                        if (options.kind && targetId) {
                            await send('/api/media/drafts/' + options.kind + '/' + targetId + '/remove/' + entry.id,
                                '{}', 'application/json', csrfToken);
                        }
                        URL.revokeObjectURL(entry.previewUrl);
                        files = files.filter(item => item !== entry);
                        if (contentInput && videoTypes.has(entry.file.type)
                            && !files.some(item => item.name === entry.name && videoTypes.has(item.file.type))) {
                            const marker = '#[' + entry.name + ']';
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
                item.append(label, state);
                if (videoTypes.has(entry.file.type) && contentInput) {
                    const insert = document.createElement('button');
                    insert.type = 'button';
                    insert.className = 'btn btn-ghost btn-xs shrink-0 text-primary';
                    insert.textContent = '본문에 삽입';
                    insert.disabled = locked || entry.removed;
                    insert.addEventListener('click', () => insertVideo(entry));
                    item.appendChild(insert);
                }
                const view = document.createElement('button');
                view.type = 'button';
                view.className = 'btn btn-ghost btn-xs shrink-0';
                view.textContent = entry.expanded ? '닫기' : '미리보기';
                view.addEventListener('click', () => { entry.expanded = !entry.expanded; render(); });
                item.append(view, remove);
                list.appendChild(item);
                if (entry.expanded) {
                    const frame = document.createElement('li');
                    frame.className = 'overflow-hidden rounded-lg border border-base-200 bg-base-100 p-2';
                    const media = document.createElement(videoTypes.has(entry.file.type) ? 'video' : 'img');
                    media.src = entry.previewUrl;
                    media.className = 'max-h-96 w-full rounded object-contain';
                    if (media.tagName === 'VIDEO') {
                        media.controls = true;
                        media.preload = 'metadata';
                        media.playsInline = true;
                        media.setAttribute('data-dico-player', '');
                    } else media.alt = entry.name;
                    frame.appendChild(media);
                    list.appendChild(frame);
                }
            });
        }

        async function prepare(file) {
            if (videoTypes.has(file.type)) {
                if (!file.size || file.size > maxVideo) throw new Error((file.name || '이미지') + ': 영상은 20MB 이하로 올려 주세요.');
                return file;
            }
            if (!imageTypes.has(file.type)) throw new Error((file.name || '이미지') + ': PNG, JPG, WebP, GIF 또는 MP4, WebM, OGG만 지원합니다.');
            if (!file.size || file.size > maxOriginalImage) throw new Error((file.name || '이미지') + ': 원본 이미지는 8MB 이하로 선택해 주세요.');
            if (file.size <= chunkSize) return file;
            if (file.type === 'image/gif') throw new Error((file.name || '이미지') + ': 움직이는 GIF는 768KB 이하로 선택해 주세요.');
            if (!window.createImageBitmap) throw new Error((file.name || '이미지') + ': 이미지를 768KB 이하로 줄여서 선택해 주세요.');
            const bitmap = await createImageBitmap(file);
            const canvas = document.createElement('canvas');
            let scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
            try {
                for (let attempt = 0; attempt < 6; attempt++) {
                    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
                    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
                    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
                    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/webp', 0.82));
                    if (blob && blob.size <= chunkSize) return new File([blob], file.name || '붙여넣은 이미지.webp', {type: 'image/webp'});
                    scale *= 0.75;
                }
            } finally {
                bitmap.close?.();
            }
            throw new Error((file.name || '이미지') + ': 이미지를 업로드 가능한 크기로 줄이지 못했습니다.');
        }

        function addFiles(selected) {
            if (locked) return pending;
            pending = pending.then(async () => {
                for (const source of Array.from(selected)) {
                    if (files.length >= 5) { announce('첨부는 최대 5개까지 가능합니다.', true); break; }
                    try {
                        const file = await prepare(source);
                        const name = typeof source.name === 'string' ? source.name.slice(0, 120) : '붙여넣은 이미지';
                        files.push({
                            id: crypto.randomUUID(), name: name || '붙여넣은 이미지',
                            file, previewUrl: URL.createObjectURL(file), nextChunk: 0, uploaded: false,
                            failed: false, removed: false, uploading: false
                        });
                        const entry = files[files.length - 1];
                        render();
                        if (videoTypes.has(file.type)) insertVideo(entry);
                        else changed();
                        if (options.ensureDraft) {
                            try {
                                await preflight();
                                targetId = await options.ensureDraft();
                                if (!entry.removed) {
                                    await uploadEntry(entry, options.kind, targetId, csrfToken);
                                    if (!entry.removed) announce(entry.name + ' 업로드를 완료했습니다.');
                                }
                            } catch (error) {
                                entry.failed = true;
                                render();
                                announce(error.message, true);
                            }
                        } else {
                            announce(files.length + '개 파일이 준비되었습니다.');
                        }
                    } catch (error) { announce(error.message, true); }
                }
            });
            return pending;
        }

        async function preflight() {
            if (!files.length) return;
            const response = await fetch("/api/media/status", {credentials: "same-origin"});
            const data = await response.json();
            if (!response.ok || !data.configured) {
                throw new Error(data.message || "서버에 postimage GitHub 토큰이 설정되지 않았습니다.");
            }
        }

        async function uploadEntry(entry, kind, id, token) {
            const base = '/api/media/' + kind + '/' + id;
            entry.uploading = true;
            entry.failed = false;
            render();
            try {
                const mime = entry.file.type;
                const name = encodeURIComponent(entry.name);
                if (imageTypes.has(mime)) {
                    announce(entry.name + ' 업로드 중…');
                    await send(base + '/images/' + entry.id + '?name=' + name, entry.file, mime, token);
                } else {
                    const total = Math.ceil(entry.file.size / chunkSize);
                    const args = '?name=' + name + '&mime=' + encodeURIComponent(mime)
                        + '&size=' + entry.file.size + '&count=' + total;
                    for (let index = entry.nextChunk; index < total && !entry.removed; index++) {
                        announce(entry.name + ' 업로드 중… ' + (index + 1) + '/' + total);
                        await send(base + '/videos/' + entry.id + '/chunks/' + index + args,
                            entry.file.slice(index * chunkSize, Math.min(entry.file.size, (index + 1) * chunkSize)),
                            mime, token);
                        entry.nextChunk = index + 1;
                    }
                    if (!entry.removed) await send(base + '/videos/' + entry.id + '/complete', '{}', 'application/json', token);
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
            for (const entry of files) {
                if (entry.uploaded || entry.removed) continue;
                await uploadEntry(entry, kind, id, token);
            }
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
            ready: () => pending,
            videoForToken,
            videos: () => files.filter(entry => !entry.removed && videoTypes.has(entry.file.type)),
            hasFiles: () => files.length > 0,
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
                pending = Promise.resolve();
                render();
                announce('작성 칸에서 Ctrl+V로 복사한 이미지도 추가할 수 있습니다.');
            }
        };
    }

    return {mount};
})();
