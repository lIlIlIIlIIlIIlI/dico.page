window.DicoFileAttachments = window.DicoFileAttachments || (() => {
    const chunkSize = 768 * 1024;
    const maxSize = 20 * 1024 * 1024;
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
                item.append(icon, label, remove);
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
                    announce(name + ': 지원하지 않는 형식이거나 20MB를 초과했습니다.', true);
                    continue;
                }
                files.push({id: crypto.randomUUID(), file, name, nextChunk: 0, uploaded: false});
                render();
                announce(files.length + '개 파일이 준비되었습니다.');
            }
        }

        async function uploadAll(kind, id, csrfToken) {
            locked = true;
            picker.disabled = true;
            render();
            for (const entry of files) {
                if (entry.uploaded) continue;
                const base = '/api/media/' + kind + '/' + id + '/files/' + entry.id;
                const total = Math.ceil(entry.file.size / chunkSize);
                const args = '?name=' + encodeURIComponent(entry.name) + '&mime=application%2Foctet-stream'
                    + '&size=' + entry.file.size + '&count=' + total;
                for (let index = entry.nextChunk; index < total; index++) {
                    announce(entry.name + ' 업로드 중… ' + (index + 1) + '/' + total);
                    await send(base + '/chunks/' + index + args,
                        entry.file.slice(index * chunkSize, Math.min(entry.file.size, (index + 1) * chunkSize)), csrfToken);
                    entry.nextChunk = index + 1;
                }
                await send(base + '/complete', '{}', csrfToken, 'application/json');
                entry.uploaded = true;
            }
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
        picker.addEventListener('change', () => { addFiles(picker.files); picker.value = ''; });
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
        const response = await fetch(url, {
            method: 'POST', body, credentials: 'same-origin',
            headers: {'Content-Type': mime, 'X-CSRF-Token': csrfToken, 'Accept': 'application/json'}
        });
        let data;
        try { data = await response.json(); } catch (_) { data = {}; }
        if (!response.ok || !data.success) {
            throw new Error(data.message || '첨부파일을 저장하지 못했습니다. 다시 시도해 주세요.');
        }
    }

    return {mount};
})();
