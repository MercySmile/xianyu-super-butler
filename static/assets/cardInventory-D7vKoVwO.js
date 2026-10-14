const s="======== CARD ========",r=`--- UPDEVNEWS MAIL ---
Username: account1
Email: account1@example.com
Password: xxx

--- PROTON ---
Email: mail1@example.com
Password: xxx

--- EPIC ---
Account: mail1@example.com
Password: xxx

======== CARD ========

--- UPDEVNEWS MAIL ---
Username: account2
Email: account2@example.com
Password: xxx

--- PROTON ---
Email: mail2@example.com
Password: xxx

--- EPIC ---
Account: mail2@example.com
Password: xxx`,m=n=>n==="data"||n==="grouped_data",x=(n,o="")=>{if(n==="data")return o.split(`
`).filter(e=>e.trim()).length;if(n!=="grouped_data")return 0;let t=0,a=!1;for(const e of o.split(/\r\n|\r|\n/))e.trim()===s?(a&&(t+=1),a=!1):e.trim()&&(a=!0);return t+(a?1:0)};export{r as G,x as g,m as i};
