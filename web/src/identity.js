// Shared anonymous identity: token + chosen name, persisted in localStorage.
export function getIdentity() {
    return {
        token: localStorage.getItem('worldsim_token'),
        name: localStorage.getItem('worldsim_name') || '',
    };
}

export async function ensureIdentity() {
    let { token } = getIdentity();
    if (!token) {
        const r = await fetch('/api/identity').then(r => r.json());
        localStorage.setItem('worldsim_token', r.token);
        token = r.token;
    }
    return token;
}

export function setIdentityName(name) {
    localStorage.setItem('worldsim_name', name);
}
