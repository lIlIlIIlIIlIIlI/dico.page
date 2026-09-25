window.DicoUploadQueue = window.DicoUploadQueue || (() => {
    function createPool(concurrency) {
        const waiting = [];
        let active = 0;

        function start() {
            while (active < concurrency && waiting.length) {
                const job = waiting.shift();
                active++;
                Promise.resolve().then(job.work).then(job.resolve, job.reject).finally(() => {
                    active--;
                    start();
                });
            }
        }

        return {add(work) {
            return new Promise((resolve, reject) => {
                waiting.push({work, resolve, reject});
                start();
            });
        }};
    }

    async function run(items, concurrency, worker) {
        let next = 0;
        let firstError = null;
        await Promise.all(Array.from({length: Math.min(concurrency, items.length)}, async () => {
            while (next < items.length) {
                const item = items[next++];
                try {
                    await worker(item);
                } catch (error) {
                    firstError ||= error;
                }
            }
        }));
        if (firstError) throw firstError;
    }

    return {createPool, run};
})();
