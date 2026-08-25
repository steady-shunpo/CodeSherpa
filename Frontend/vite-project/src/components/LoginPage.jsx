import { useState, useEffect } from "react";
import { Zap } from "lucide-react";
import { initLogin } from "../utils/api";
import { useNavigate } from "react-router-dom";

const GoogleIcon = () => (
  <svg width="16" height="16" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
    <path d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844a4.14 4.14 0 01-1.796 2.716v2.259h2.908C16.658 14.252 17.64 11.926 17.64 9.2z" fill="#4285F4"/>
    <path d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 009 18z" fill="#34A853"/>
    <path d="M3.964 10.71A5.41 5.41 0 013.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 000 9c0 1.452.348 2.827.957 4.042l3.007-2.332z" fill="#FBBC05"/>
    <path d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 00.957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z" fill="#EA4335"/>
  </svg>
);

const GitHubIcon = () => (
  <svg width="16" height="16" viewBox="0 0 18 18" fill="currentColor" xmlns="http://www.w3.org/2000/svg">
    <path d="M9 0C4.03 0 0 4.03 0 9c0 3.978 2.578 7.352 6.155 8.543.45.083.614-.195.614-.434 0-.214-.008-.78-.012-1.532-2.504.544-3.032-1.207-3.032-1.207-.41-1.04-1-1.317-1-1.317-.817-.559.062-.547.062-.547.903.063 1.38.928 1.38.928.802 1.374 2.105.978 2.618.748.082-.582.314-.978.57-1.202-1.999-.228-4.1-1-4.1-4.45 0-.983.35-1.787.928-2.418-.093-.228-.403-1.144.088-2.385 0 0 .756-.242 2.478.924A8.636 8.636 0 019 4.381c.766.003 1.537.104 2.258.304 1.72-1.166 2.474-.924 2.474-.924.493 1.241.183 2.157.09 2.385.578.631.926 1.435.926 2.418 0 3.458-2.104 4.22-4.108 4.443.323.278.61.828.61 1.669 0 1.204-.011 2.175-.011 2.472 0 .24.162.52.618.432C15.426 16.348 18 12.977 18 9c0-4.97-4.03-9-9-9z"/>
  </svg>
);

const LinkedInIcon = () => (
  <svg width="16" height="16" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
    <rect width="18" height="18" rx="4" fill="#0A66C2"/>
    <path d="M5.5 7H4v7h1.5V7zm-.75-2.4a.9.9 0 100 1.8.9.9 0 000-1.8zM14 10.5c0-1.933-.867-3.5-2.6-3.5A2.4 2.4 0 009.5 8.1V7H8v7h1.5v-3.5c0-1.1.5-1.9 1.5-1.9.9 0 1.5.7 1.5 1.9V14H14v-3.5z" fill="white"/>
  </svg>
);

const providers = [
  { id: "google", label: "Continue with Google", Icon: GoogleIcon },
  { id: "github", label: "Continue with GitHub", Icon: GitHubIcon },
  { id: "linkedin", label: "Continue with LinkedIn", Icon: LinkedInIcon },
];

export default function LoginPage() {
  const [loading, setLoading] = useState(null);
  const navigate = useNavigate();

  useEffect(() => {
    const root = document.documentElement;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = (dark) => root.classList.toggle("dark", dark);
    apply(mq.matches);
    mq.addEventListener("change", (e) => apply(e.matches));
    return () => mq.removeEventListener("change", (e) => apply(e.matches));
  }, []);

  const handleOAuth = (id) => {
  initLogin(id);
};

  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-8 p-10 animate-in fade-in duration-300">
      <div className="flex flex-col items-center gap-3 text-center">
        <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full border border-primary/40 bg-primary/10 text-primary text-xs font-medium">
          <Zap size={11} /> Secure sign-in
        </span>
        <h1 className="text-4xl font-bold tracking-tight text-foreground leading-tight">
          Welcome back.
        </h1>
        <p className="text-muted-foreground text-base max-w-sm leading-relaxed">
          Sign in to your account to continue.
        </p>
      </div>

      <div className="w-full max-w-xs flex flex-col gap-2">
        {providers.map(({ id, label, Icon }) => (
          <button
            key={id}
            onClick={() => handleOAuth(id)}
            disabled={loading !== null}
            className="flex items-center gap-3 px-4 py-2.5 bg-card border border-border rounded-xl text-sm font-medium text-foreground hover:border-ring hover:bg-muted/40 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Icon />
            {loading === id ? "Redirecting…" : label}
          </button>
        ))}
      </div>

      <p className="text-[10px] uppercase tracking-widest text-muted-foreground/50 text-center max-w-xs leading-relaxed">
        By continuing, you agree to our{" "}
        <a href="#" className="underline underline-offset-2 hover:text-muted-foreground transition-colors">Terms</a>
        {" "}and{" "}
        <a href="#" className="underline underline-offset-2 hover:text-muted-foreground transition-colors">Privacy Policy</a>
      </p>
    </div>
  );
}