//! CPython's `random.Random`, bit for bit: the same seed gives the same shuffles and picks as
//! the Python engine, which is what lets the two engines be checked against each other.

const N: usize = 624;
const M: usize = 397;

#[derive(Clone)]
pub struct PyRandom {
    mt: [u32; N],
    idx: usize,
}

impl PyRandom {
    /// `random.Random(seed)` for a non-negative integer seed (the key is the seed's 32-bit words).
    pub fn new(seed: u64) -> Self {
        let mut key = vec![seed as u32];
        if seed >> 32 != 0 {
            key.push((seed >> 32) as u32);
        }
        let mut r = PyRandom { mt: [0; N], idx: N + 1 };
        r.init_by_array(&key);
        r
    }

    fn init_genrand(&mut self, s: u32) {
        self.mt[0] = s;
        for i in 1..N {
            let prev = self.mt[i - 1];
            self.mt[i] = 1812433253u32.wrapping_mul(prev ^ (prev >> 30)).wrapping_add(i as u32);
        }
        self.idx = N;
    }

    fn init_by_array(&mut self, key: &[u32]) {
        self.init_genrand(19650218);
        let (mut i, mut j) = (1usize, 0usize);
        let mut k = N.max(key.len());
        while k > 0 {
            let prev = self.mt[i - 1];
            self.mt[i] = (self.mt[i] ^ (prev ^ (prev >> 30)).wrapping_mul(1664525))
                .wrapping_add(key[j])
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= N {
                self.mt[0] = self.mt[N - 1];
                i = 1;
            }
            if j >= key.len() {
                j = 0;
            }
            k -= 1;
        }
        k = N - 1;
        while k > 0 {
            let prev = self.mt[i - 1];
            self.mt[i] = (self.mt[i] ^ (prev ^ (prev >> 30)).wrapping_mul(1566083941)).wrapping_sub(i as u32);
            i += 1;
            if i >= N {
                self.mt[0] = self.mt[N - 1];
                i = 1;
            }
            k -= 1;
        }
        self.mt[0] = 0x8000_0000;
    }

    fn genrand(&mut self) -> u32 {
        const UPPER: u32 = 0x8000_0000;
        const LOWER: u32 = 0x7fff_ffff;
        const MATRIX: u32 = 0x9908_b0df;
        if self.idx >= N {
            for kk in 0..N {
                let y = (self.mt[kk] & UPPER) | (self.mt[(kk + 1) % N] & LOWER);
                let mag = if y & 1 == 1 { MATRIX } else { 0 };
                self.mt[kk] = self.mt[(kk + M) % N] ^ (y >> 1) ^ mag;
            }
            self.idx = 0;
        }
        let mut y = self.mt[self.idx];
        self.idx += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^= y >> 18;
        y
    }

    /// `getrandbits(k)` for 1 <= k <= 32.
    fn getrandbits(&mut self, k: u32) -> u32 {
        self.genrand() >> (32 - k)
    }

    /// `_randbelow(n)`: rejection sampling on `n.bit_length()` bits.
    pub fn randbelow(&mut self, n: usize) -> usize {
        assert!(n > 0 && n <= u32::MAX as usize);
        let k = usize::BITS - n.leading_zeros();
        loop {
            let r = self.getrandbits(k) as usize;
            if r < n {
                return r;
            }
        }
    }

    /// `randrange(n)`.
    pub fn randrange(&mut self, n: usize) -> usize {
        self.randbelow(n)
    }

    /// `shuffle(x)` (Fisher-Yates from the end).
    pub fn shuffle<T>(&mut self, x: &mut [T]) {
        for i in (1..x.len()).rev() {
            let j = self.randbelow(i + 1);
            x.swap(i, j);
        }
    }

    /// `sample(population, k)` (CPython 3.10's algorithm, both branches).
    pub fn sample<T: Clone>(&mut self, population: &[T], k: usize) -> Vec<T> {
        let n = population.len();
        assert!(k <= n, "sample larger than population");
        let mut setsize = 21usize;
        if k > 5 {
            // 4 ** ceil(log(k * 3, 4))
            let e = ((k * 3) as f64).ln() / 4f64.ln();
            setsize += 4usize.pow(e.ceil() as u32);
        }
        let mut out = Vec::with_capacity(k);
        if n <= setsize {
            let mut pool: Vec<T> = population.to_vec();
            for i in 0..k {
                let j = self.randbelow(n - i);
                out.push(pool[j].clone());
                pool[j] = pool[n - i - 1].clone();
            }
        } else {
            let mut selected = std::collections::HashSet::new();
            for _ in 0..k {
                let mut j = self.randbelow(n);
                while selected.contains(&j) {
                    j = self.randbelow(n);
                }
                selected.insert(j);
                out.push(population[j].clone());
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Expected values printed by CPython 3.10's random module.
    #[test]
    fn matches_cpython() {
        assert_eq!(PyRandom::new(42).genrand(), 2746317213);
        assert_eq!(PyRandom::new(0).genrand(), 3626764237);
        let mut r = PyRandom::new(7);
        let mut v: Vec<u32> = (0..10).collect();
        r.shuffle(&mut v);
        assert_eq!(v, vec![8, 3, 1, 4, 7, 0, 9, 6, 2, 5]);
        let mut r = PyRandom::new((1 << 40) + 5);
        let bits: Vec<usize> = (0..8).map(|_| r.randrange(2)).collect();
        assert_eq!(bits, vec![1, 0, 0, 0, 0, 0, 0, 0]);
        let pop: Vec<u32> = (0..12).collect();
        assert_eq!(r.sample(&pop, 4), vec![3, 10, 6, 1]);
        let pop: Vec<u32> = (0..30).collect();
        assert_eq!(r.sample(&pop, 3), vec![25, 17, 7]);
    }
}
