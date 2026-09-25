(() => {
    const chunkSize = 768 * 1024;

    document.addEventListener('click', async event => {
        const link = event.target.closest('a[data-dico-download]');
        if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        if (link.getAttribute('aria-busy') === 'true') return;
        link.setAttribute('aria-busy', 'true');
        const label = link.textContent;

        try {
            const head = await fetch(link.href, {method: 'HEAD'});
            const size = Number(head.headers.get('Content-Length'));
            if (!head.ok || !Number.isSafeInteger(size) || size <= 0) throw new Error('파일 정보를 가져오지 못했습니다.');
            const parts = [];
            for (let start = 0; start < size; start += chunkSize) {
                const end = Math.min(size - 1, start + chunkSize - 1);
                link.textContent = '다운로드 중… ' + Math.floor(start / size * 100) + '%';
                const response = await fetch(link.href, {headers: {'Range': 'bytes=' + start + '-' + end}});
                if (response.status !== 206 && !(response.status === 200 && start === 0 && end === size - 1)) {
                    throw new Error('파일의 일부를 가져오지 못했습니다.');
                }
                const part = await response.arrayBuffer();
                if (part.byteLength !== end - start + 1) throw new Error('다운로드한 파일 크기가 올바르지 않습니다.');
                parts.push(part);
            }
            const url = URL.createObjectURL(new Blob(parts, {type: head.headers.get('Content-Type') || 'application/octet-stream'}));
            const download = document.createElement('a');
            download.href = url;
            download.download = link.download;
            download.click();
            window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
        } catch (error) {
            window.alert(error.message || '첨부파일을 다운로드하지 못했습니다.');
        } finally {
            link.textContent = label;
            link.removeAttribute('aria-busy');
        }
    });
})();
