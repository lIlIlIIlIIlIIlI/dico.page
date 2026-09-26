window.DicoFileAttachments = window.DicoFileAttachments || (() => {
    const wireChunkSize = 4 * 1024 * 1024;
    const chunkSize = 50 * 1024 * 1024;
    const maxSize = 100 * 1024 * 1024;
    const extensions = new Set(['pdf', 'zip', 'txt', 'csv', 'hwp', 'hwpx', 'docx', 'xlsx', 'pptx']);

    function mount(form) {
        const root = form.querySelector('[data-file-attachments]');
        if (!root) return null;
        const picker = root.querySelector('[data-file-picker]');
        const zone = root.querySelector('[data-file-dropzone]');
        const list = root.querySelector('[data-file-list]');
        const count = root.querySelector('[data-file-count]');
        const status = root.querySelector('[data-file-status]');
        let files = [];
        let locked = false;

        function announce(message, error = false) {
            status.textContent = message;
            status.classList.toggle('text-error', error);
        }

        function render() {
            count.textContent = files.length + '/5';
            list.replaceChildren();
            files.forEach((entry, index) => {
                const item = document.createElement('li');
                item.className = 'flex min-w-0 items-center gap-3 rounded-lg border border-base-200 bg-base-200/30 p-2';
                const icon = document.createElement('span');
                icon.className = 'flex size-10 shrink-0 items-center justify-center rounded bg-primary/10 text-xs font-semibold text-primary';
                icon.textContent = entry.name.split('.').pop().toUpperCase();
                const label = document.createElement('span');
                label.className = 'min-w-0 flex-1 truncate text-sm';
                label.textContent = entry.name;
                label.title = entry.name;
                const state = document.createElement('span');
                state.className = 'shrink-0 text-xs text-base-content/55';
                state.textContent = entry.uploaded ? '업로드 완료' : entry.failed ? '업로드 실패' : entry.progress || '준비 중';
                const remove = document.createElement('button');
                remove.type = 'button';
                remove.className = 'btn btn-ghost btn-xs shrink-0 text-error';
                remove.textContent = '삭제';
                remove.disabled = locked;
                remove.addEventListener('click', () => {
                    if (locked) return;
                    files.splice(index, 1);
                    render();
                });
                item.append(icon, label, state, remove);
                list.appendChild(item);
            });
        }

        function addFiles(selected) {
            if (locked) return;
            for (const file of Array.from(selected)) {
                if (files.length >= 5) { announce('일반 첨부파일은 최대 5개까지 가능합니다.', true); break; }
                const name = file.name.trim();
                const extension = name.split('.').pop().toLowerCase();
                if (!extensions.has(extension) || name.length > 120 || !file.size || file.size > maxSize) {
                    announce(name + ': 지원하지 않는 형식이거나 100MB를 초과했습니다.', true);
                    continue;
                }
                files.push({id: crypto.randomUUID(), file, name, completedChunks: new Set(), uploaded: false});
                render();
                announce(files.length + '개 파일이 준비되었습니다.');
            }
        }

        async function uploadAll(kind, id, csrfToken) {
            locked = true;
            picker.disabled = true;
            render();
            await window.DicoUploadQueue.run(files.filter(entry => !entry.uploaded), 1, async entry => {
                const base = '/api/media/' + kind + '/' + id + '/files/' + entry.id;
                const total = Math.ceil(entry.file.size / chunkSize);
                const args = '?name=' + encodeURIComponent(entry.name) + '&mime=application%2Foctet-stream'
                    + '&size=' + entry.file.size + '&count=' + total + '&chunk_size=' + chunkSize;
                entry.failed = false;
                try {
                    const statusResponse = await fetch(base + '/status', {
                        credentials: 'same-origin', headers: {'X-CSRF-Token': csrfToken, 'Accept': 'application/json'}
                    });
                    const status = await statusResponse.json();
                    if (!statusResponse.ok || !status.success) throw new Error(status.message || '업로드 상태를 확인하지 못했습니다.');
                    if (status.complete) { entry.uploaded = true; render(); return; }
                    entry.completedChunks = new Set(status.uploaded_chunks || []);
                    const chunksPerLogical = Math.ceil(chunkSize / wireChunkSize);
                    const digest = window.DicoSha256.create();
                    for (let logicalIndex = 0; logicalIndex < total; logicalIndex++) {
                        const groupStart = logicalIndex * chunkSize;
                        const groupSize = Math.min(chunkSize, entry.file.size - groupStart);
                        for (let offset = 0; offset < groupSize; offset += wireChunkSize) {
                            const start = groupStart + offset;
                            const end = Math.min(groupStart + groupSize, start + wireChunkSize);
                            const body = new Uint8Array(await entry.file.slice(start, end).arrayBuffer());
                            digest.update(body);
                            if (entry.completedChunks.has(logicalIndex)) continue;
                            const index = logicalIndex * chunksPerLogical + offset / wireChunkSize;
                            const query = args + '&chunk_index=' + logicalIndex + '&chunk_offset=' + offset;
                            const result = await send(base + '/chunks/' + index + query, body, csrfToken);
                            entry.completedChunks = new Set(result.uploaded_chunks || entry.completedChunks);
                        }
                        entry.progress = '업로드 중 ' + Math.max(logicalIndex + 1, entry.completedChunks.size) + '/' + total;
                        render();
                    }
                    await send(base + '/complete', JSON.stringify({sha256: digest.digest()}), csrfToken, 'application/json');
                    entry.uploaded = true;
                    render();
                } catch (error) {
                    entry.failed = true;
                    render();
                    throw error;
                }
            });
            announce('일반 첨부파일을 저장했습니다.');
        }

        function clear() {
            files = [];
            locked = false;
            picker.disabled = false;
            render();
            announce('파일은 게시글·공지 등록 후 별도로 저장됩니다.');
        }

        root.querySelector('[data-file-select]').addEventListener('click', () => picker.click());
        picker.addEventListener('change', () => {
            const selected = Array.from(picker.files);
            picker.value = '';
            addFiles(selected);
        });
        zone.addEventListener('click', () => { if (!locked) picker.click(); });
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

        return {
            preflight: async () => {
                if (!files.length) return;
                const response = await fetch('/api/media/status', {credentials: 'same-origin'});
                const data = await response.json();
                if (!response.ok || !data.configured) {
                    throw new Error(data.message || '서버에 postimage GitHub 토큰이 설정되지 않았습니다.');
                }
            },
            uploadAll,
            clear
        };
    }

    async function send(url, body, csrfToken, mime = 'application/octet-stream') {
        for (let attempt = 1; attempt <= 8; attempt++) {
            let response;
            let data;
            try {
                response = await fetch(url, {
                    method: 'POST', body, credentials: 'same-origin',
                    headers: {'Content-Type': mime, 'X-CSRF-Token': csrfToken, 'Accept': 'application/json'}
                });
                try { data = await response.json(); } catch (_) { data = {}; }
            } catch (error) {
                if (attempt === 8) throw error;
                await new Promise(resolve => setTimeout(resolve, (Math.min(60, 2 ** (attempt - 1)) + Math.random() * 0.5) * 1000));
                continue;
            }
            if (response.ok && data.success) return data;
            const retryable = response.status === 403 || response.status === 429 || response.status >= 500;
            if (retryable && attempt < 8) {
                const backoff = Math.min(60, 2 ** (attempt - 1)) + Math.random() * 0.5;
                const bodyDelay = Number(data.retry_after) || 0;
                const retryHeader = response.headers?.get?.('Retry-After');
                const parsedHeader = Number(retryHeader);
                const retryDate = Date.parse(retryHeader || '');
                const headerDelay = Number.isFinite(parsedHeader) ? parsedHeader
                    : Number.isFinite(retryDate) ? Math.max(0, (retryDate - Date.now()) / 1000) : 0;
                await new Promise(resolve => setTimeout(resolve, Math.min(3600, Math.max(backoff, bodyDelay, headerDelay)) * 1000));
                continue;
            }
            throw new Error(data.message || '첨부파일을 저장하지 못했습니다. 다시 시도해 주세요.');
        }
    }

    return {mount};
})();
