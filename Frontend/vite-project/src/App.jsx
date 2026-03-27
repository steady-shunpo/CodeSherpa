import { AppProvider } from './store/appStore';
import MainLayout from './components/MainLayout';

export default function App() {
  return (
    <AppProvider>
      <MainLayout />
    </AppProvider>
  );
}