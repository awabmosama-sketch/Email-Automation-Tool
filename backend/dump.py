p=r"D:\Abdion\Email Automation\email-automation\frontend\index.html"
d=open(p,encoding='utf-8').read()
i=d.find('Reference documents')
if i>=0:
    print(repr(d[i-200:i+120]))
else:
    print('not found')
