import { jwtDecode } from 'jwt-decode';

/**
 * Decodes the JWT stored in localStorage to read display-only user info
 * (name, email, picture). This does NOT verify the signature -- never use
 * this for authorization decisions. The backend's decode_jwt (which does
 * verify) remains the source of truth for anything security-relevant.
 */
export function getCurrentUser() {
  const token = localStorage.getItem('token');
  if (!token) return null;

  try {
    const payload = jwtDecode(token);
    // also catches expired tokens client-side, so the UI can react
    // before the next API call would 401
    if (payload.exp && payload.exp * 1000 < Date.now()) {
      return null;
    }
    return {
      email: payload.email,
      name: payload.name,
      picture: payload.picture,
    };
  } catch {
    return null;
  }
}

export function logout(navigate) {
  localStorage.removeItem('token');
  navigate('/login', { replace: true });
}