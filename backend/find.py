path = r"D:\Abdion\Email Automation\email-automation\frontend\index.html"
with open(path, encoding='utf-8') as f:
    for i,l in enumerate(f,1):
        if 'Reference documents' in l:
            print(i)
            break
    else:
        print(-1)
