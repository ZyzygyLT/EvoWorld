import React from 'react';

export default class ErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false, error: null };
    }
    static getDerivedStateFromError(error) {
        return { hasError: true, error };
    }
    componentDidCatch(error, info) {
        console.error('ErrorBoundary:', error, info);
    }
    render() {
        if (this.state.hasError) {
            return (
                <div style={{
                    display: 'flex', flexDirection: 'column', alignItems: 'center',
                    justifyContent: 'center', height: '100%', padding: 24,
                    background: '#0d1117', color: '#f85149', textAlign: 'center',
                }}>
                    <div style={{ fontSize: 18, marginBottom: 8 }}>Something went wrong</div>
                    <div style={{ fontSize: 12, color: '#8b949e', marginBottom: 12, maxWidth: 480, wordBreak: 'break-word' }}>
                        {String(this.state.error?.message || this.state.error || '')}
                    </div>
                    <button
                        style={{
                            background: '#21262d', border: '1px solid #30363d',
                            color: '#c9d1d9', borderRadius: 4, padding: '6px 16px', cursor: 'pointer',
                        }}
                        onClick={() => this.setState({ hasError: false, error: null })}
                    >Try again</button>
                    <button
                        style={{
                            background: '#21262d', border: '1px solid #30363d',
                            color: '#8b949e', borderRadius: 4, padding: '6px 16px', cursor: 'pointer', marginLeft: 8,
                        }}
                        onClick={() => location.reload()}
                    >Reload page</button>
                </div>
            );
        }
        return this.props.children;
    }
}
