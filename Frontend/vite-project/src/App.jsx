import { AppProvider } from './store/appStore';
import { Routes, Route } from 'react-router-dom';

import MainLayout from './components/MainLayout';
import LoginPage from './components/LoginPage';
import AuthSuccess from './components/AuthSuccess';
import ProtectedRoute from './components/ProtectedRoute';

export default function App() {
  return (
    <AppProvider>
      <Routes>
        <Route path="/" element={
          // <ProtectedRoute><MainLayout /></ProtectedRoute>
          <MainLayout />
        } />
        <Route path="/runs/:runId" element={
          // <ProtectedRoute><MainLayout /></ProtectedRoute>
          <MainLayout />
        } />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/auth/success" element={<AuthSuccess />} />
      </Routes>
    </AppProvider>
  );
}