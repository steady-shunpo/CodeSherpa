import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';

export default function AuthSuccess() {
  const navigate = useNavigate();
  const hasRun = useRef(false);

  useEffect(() => {
    if (hasRun.current) return;   // prevents StrictMode double-invoke (and any remount) from re-running this
    hasRun.current = true;

    const token = new URLSearchParams(window.location.search).get('token');

    if (token) {
      localStorage.setItem('token', token);
      navigate('/', { replace: true });
    } else {
      navigate('/login?error=auth_failed', { replace: true });
    }
  }, []);

  return <p>Signing you in...</p>;
}