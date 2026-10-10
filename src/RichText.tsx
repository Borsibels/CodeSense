import Markdown from 'react-markdown';

/** AI prose only. Raw HTML, images and arbitrary source links are never activated. */
export function RichText({ text }: { text: string }) {
  return <div className="rich-text"><Markdown skipHtml components={{
    a: ({ children }) => <span>{children}</span>,
    img: ({ alt }) => <span>{alt || ''}</span>,
  }}>{text}</Markdown></div>;
}
