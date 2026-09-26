window.DicoSha256 = window.DicoSha256 || (() => {
    const constants = new Uint32Array([
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ]);

    function rotate(value, bits) { return (value >>> bits) | (value << (32 - bits)); }

    class Sha256 {
        constructor() {
            this.state = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
            this.words = new Uint32Array(64);
            this.buffer = new Uint8Array(64);
            this.bufferLength = 0;
            this.byteLength = 0n;
            this.finished = false;
        }

        update(input) {
            if (this.finished) throw new Error('SHA-256 is already finalized.');
            const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
            this.byteLength += BigInt(bytes.byteLength);
            let offset = 0;
            if (this.bufferLength) {
                const copied = Math.min(64 - this.bufferLength, bytes.length);
                this.buffer.set(bytes.subarray(0, copied), this.bufferLength);
                this.bufferLength += copied;
                offset = copied;
                if (this.bufferLength === 64) {
                    this.compress(this.buffer);
                    this.bufferLength = 0;
                }
            }
            while (offset + 64 <= bytes.length) {
                this.compress(bytes.subarray(offset, offset + 64));
                offset += 64;
            }
            if (offset < bytes.length) {
                this.buffer.set(bytes.subarray(offset), 0);
                this.bufferLength = bytes.length - offset;
            }
            return this;
        }

        compress(block) {
            const words = this.words;
            const view = new DataView(block.buffer, block.byteOffset, 64);
            for (let i = 0; i < 16; i++) words[i] = view.getUint32(i * 4, false);
            for (let i = 16; i < 64; i++) {
                const x = words[i - 15], y = words[i - 2];
                const s0 = rotate(x, 7) ^ rotate(x, 18) ^ (x >>> 3);
                const s1 = rotate(y, 17) ^ rotate(y, 19) ^ (y >>> 10);
                words[i] = (words[i - 16] + s0 + words[i - 7] + s1) >>> 0;
            }
            let [a, b, c, d, e, f, g, h] = this.state;
            for (let i = 0; i < 64; i++) {
                const sum1 = rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25);
                const choose = (e & f) ^ (~e & g);
                const t1 = (h + sum1 + choose + constants[i] + words[i]) >>> 0;
                const sum0 = rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22);
                const majority = (a & b) ^ (a & c) ^ (b & c);
                const t2 = (sum0 + majority) >>> 0;
                h = g; g = f; f = e; e = (d + t1) >>> 0;
                d = c; c = b; b = a; a = (t1 + t2) >>> 0;
            }
            this.state[0] = (this.state[0] + a) >>> 0;
            this.state[1] = (this.state[1] + b) >>> 0;
            this.state[2] = (this.state[2] + c) >>> 0;
            this.state[3] = (this.state[3] + d) >>> 0;
            this.state[4] = (this.state[4] + e) >>> 0;
            this.state[5] = (this.state[5] + f) >>> 0;
            this.state[6] = (this.state[6] + g) >>> 0;
            this.state[7] = (this.state[7] + h) >>> 0;
        }

        digest() {
            if (this.finished) throw new Error('SHA-256 is already finalized.');
            const bitLength = this.byteLength * 8n;
            const paddedLength = this.bufferLength < 56 ? 64 : 128;
            const padding = new Uint8Array(paddedLength - this.bufferLength);
            padding[0] = 0x80;
            for (let i = 0; i < 8; i++) padding[padding.length - 1 - i] = Number((bitLength >> BigInt(i * 8)) & 0xffn);
            this.update(padding);
            this.finished = true;
            return Array.from(this.state, word => word.toString(16).padStart(8, '0')).join('');
        }
    }

    return {create: () => new Sha256()};
})();
