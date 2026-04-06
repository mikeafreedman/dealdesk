import streamlit as st

st.set_page_config(
    page_title='Deal Desk — AUS',
    page_icon='🏢',
    layout='wide'
)

st.title('Deal Desk')
st.subheader('Automated Underwriting System')
st.write('System is online. Ready for underwriting.')

# Test API key access
try:
    key = st.secrets['ANTHROPIC_API_KEY']
    st.success(f'API key loaded. First 10 chars: {key[:10]}...')
except Exception as e:
    st.error(f'Could not load API key: {e}')
