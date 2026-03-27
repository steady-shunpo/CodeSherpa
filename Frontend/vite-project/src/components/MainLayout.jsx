import { useApp } from '../store/appStore';
import Navbar from './Navbar';
import Sidebar from './Sidebar';
import IdleScreen from './IdleScreen';
import LoadingScreen from './LoadingScreen';
import ChatPanel from './ChatPanel';
import PipelinePanel from './PipelinePanel';

export default function MainLayout() {
  const { state } = useApp();
  const { phase } = state;

  const showPipeline = phase === 'pipeline';

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      <Navbar />
      <div className="flex flex-1 min-h-0 overflow-hidden">
        <Sidebar />

        {/* Main area */}
        <div className="flex flex-1 min-w-0 overflow-hidden">

          {phase === 'idle' && <IdleScreen />}
          {phase === 'loading' && <LoadingScreen />}

          {(phase === 'chat' || phase === 'pipeline') && (
            <>
              {/* Chat always visible */}
              <div className={`flex flex-col min-h-0 overflow-hidden transition-all duration-300 ${showPipeline ? 'flex-1' : 'flex-1'}`}>
                <ChatPanel />
              </div>

              {/* Pipeline panel slides in from right */}
              <div
                className="flex flex-col shrink-0 border-l border-border bg-card overflow-hidden transition-all duration-300 ease-in-out"
                style={{ width: showPipeline ? 360 : 0, minWidth: showPipeline ? 360 : 0 }}
              >
                {showPipeline && <PipelinePanel />}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}