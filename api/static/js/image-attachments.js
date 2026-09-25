window.DicoImageAttachments = window.DicoImageAttachments || (() => {
    const maxImages = 5;
    const maxBytes = 512 * 1024;
    const maxTotal = 2 * 1024 * 1024;
    const maxOriginal = 8 * 1024 * 1024;
    const allowed = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);

    function mount(form) {
        const root = form.querySelector('[data-image-attachments]');
        if (!root) return null;
        const picker = root.querySelector('[data-image-picker]');
        const zone = root.querySelector('[data-image-dropzone]');
        const list = root.querySelector('[data-image-list]');
        const input = root.querySelector('[data-image-value]');
        const count = root.querySelector('[data-image-count]');
        const status = root.querySelector('[data-image-status]');
        const menu = root.querySelector('[data-image-menu]');
        let images = [];
        let pending = Promise.resolve();

        function announce(message, error = false) {
            status.textContent = message;
            status.classList.toggle('text-error', error);
        }

        function render() {
            input.value = JSON.stringify(images.map(({name, data}) => ({name, data})));
            count.textContent = `${images.length}/${maxImages}`;
            list.replaceChildren();
            images.forEach((image, index) => {
                const item = document.createElement('li');
                item.className = 'flex min-w-0 items-center gap-3 rounded-lg border border-base-200 bg-base-200/30 p-2';
                const thumb = document.createElement('img');
                thumb.src = image.data;
                thumb.alt = '';
                thumb.className = 'size-10 shrink-0 rounded object-cover';
                const name = document.createElement('span');
                name.className = 'min-w-0 flex-1 truncate text-sm';
                name.title = image.name;
                name.textContent = image.name;
                const remove = document.createElement('button');
                remove.type = 'button';
                remove.className = 'btn btn-ghost btn-xs shrink-0 text-error';
                remove.textContent = '삭제';
                remove.setAttribute('aria-label', `${image.name} 삭제`);
                remove.addEventListener('click', () => {
                    images.splice(index, 1);
                    render();
                    announce('이미지를 목록에서 삭제했습니다.');
                });
                item.append(thumb, name, remove);
                list.append(item);
            });
        }

        function readFile(file) {
            return new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject(new Error('이미지를 읽지 못했습니다.'));
                reader.readAsDataURL(file);
            });
        }

        async function prepareFile(file) {
            if (!file.size || file.size > maxOriginal) throw new Error(`${file.name}: 원본 이미지는 8MB 이하로 선택해 주세요.`);
            if (file.size <= maxBytes) return {data: await readFile(file), size: file.size};
            if (file.type === 'image/gif') throw new Error(`${file.name}: 움직이는 GIF를 유지하려면 512KB 이하로 선택해 주세요.`);
            if (!window.createImageBitmap) throw new Error(`${file.name}: 이미지를 512KB 이하로 줄여서 선택해 주세요.`);
            const bitmap = await createImageBitmap(file);
            const canvas = document.createElement('canvas');
            let scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
            try {
                for (let attempt = 0; attempt < 5; attempt++) {
                    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
                    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
                    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
                    let data = canvas.toDataURL('image/webp', 0.82);
                    if (!data.startsWith('data:image/webp;base64,')) data = canvas.toDataURL('image/jpeg', 0.78);
                    const size = Math.floor((data.split(',')[1].length * 3) / 4);
                    if (size <= maxBytes) return {data, size};
                    scale *= 0.75;
                }
            } finally {
                bitmap.close?.();
            }
            throw new Error(`${file.name}: 이미지를 512KB 이하로 변환하지 못했습니다.`);
        }

        function addFiles(files) {
            pending = pending.then(async () => {
                for (const file of Array.from(files)) {
                    if (!allowed.has(file.type)) {
                        announce(`${file.name}: PNG, JPG, WebP, GIF 이미지만 첨부할 수 있습니다.`, true);
                        continue;
                    }
                    if (images.length >= maxImages) {
                        announce('이미지는 5개, 총 2MB 이하만 첨부할 수 있습니다.', true);
                        break;
                    }
                    try {
                        const {data, size} = await prepareFile(file);
                        if (images.reduce((sum, image) => sum + image.size, 0) + size > maxTotal) {
                            announce('이미지의 총 저장 용량은 2MB 이하입니다.', true);
                            continue;
                        }
                        images.push({name: file.name.slice(0, 120), data, size});
                        render();
                        announce(`${images.length}개 이미지가 준비되었습니다.`);
                    } catch (error) {
                        announce(error.message, true);
                    }
                }
            });
            return pending;
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
                    const files = [];
                    for (const entry of entries) {
                        const type = entry.types.find(type => allowed.has(type));
                        if (type) files.push(await entry.getType(type));
                    }
                    if (files.length) { addFiles(files); return; }
                } catch (_) {}
            }
            zone.focus();
            announce('복사한 이미지를 Ctrl+V로 붙여넣어 주세요.');
        });
        picker.addEventListener('change', () => {
            addFiles(picker.files);
            picker.value = '';
        });
        zone.addEventListener('click', () => picker.click());
        form.querySelector('[data-image-open]')?.addEventListener('click', () => {
            menu.open = true;
            root.scrollIntoView({block: 'nearest', behavior: 'smooth'});
        });
        zone.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                picker.click();
            }
        });
        zone.addEventListener('dragover', event => {
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
            const files = Array.from(event.clipboardData?.items || [])
                .filter(item => item.kind === 'file' && item.type.startsWith('image/'))
                .map(item => item.getAsFile()).filter(Boolean);
            if (!files.length) return;
            event.preventDefault();
            addFiles(files);
        });

        return {
            ready: () => pending,
            clear: () => { images = []; render(); announce('이미지 목록을 비웠습니다.'); }
        };
    }

    return {mount};
})();
