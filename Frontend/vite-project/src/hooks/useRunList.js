import { useEffect } from 'react';
import { useApp } from '../store/appStore';
import { listRuns } from '../utils/api';

export function useRunsList() {
  const { dispatch } = useApp();

  useEffect(() => {
    async function fetch() {
      dispatch({ type: 'RUNS_LOADING' });
      try {
        const runs = await listRuns();
        dispatch({ type: 'RUNS_LOADED', runs });
      } catch (err) {
        dispatch({ type: 'RUNS_ERROR' });
      }
    }
    fetch();
  }, []);
}